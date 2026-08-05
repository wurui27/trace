import asyncio
import base64
import binascii
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from perfpilot_api.storage.base import (
    ArtifactAuthorizationError,
    ArtifactMetadataError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    GetAuthorization,
    CompletedMultipart,
    MultipartCreation,
    MultipartPart,
    MultipartPartAuthorization,
    ObjectLocation,
    PutAuthorization,
    StoredObjectMetadata,
)


_NOT_FOUND_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NoSuchVersion", "NotFound"})


def _is_nonempty_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _is_sha256_b64(value: object) -> bool:
    if not _is_nonempty_text(value):
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == 32 and base64.b64encode(decoded).decode("ascii") == value


def _is_immutable_version_id(value: object) -> bool:
    return _is_nonempty_text(value) and value != "null"


def _valid_ttl(value: object, *, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    error_details = response.get("Error")
    if not isinstance(error_details, Mapping):
        return None
    code = error_details.get("Code")
    return str(code) if code is not None else None


class S3ArtifactStore:
    """Create bounded S3 authorizations and inspect immutable object versions."""

    def __init__(self, *, client: Any) -> None:
        self._client = client

    @staticmethod
    def _valid_location(location: ObjectLocation) -> bool:
        return _is_nonempty_text(location.bucket) and _is_nonempty_text(location.key)

    async def authorize_put(
        self,
        *,
        location: ObjectLocation,
        content_type: str,
        checksum_sha256_b64: str,
        expires_in_seconds: int = 900,
    ) -> PutAuthorization:
        if (
            not self._valid_location(location)
            or location.version_id is not None
            or not _is_nonempty_text(content_type)
            or not _is_sha256_b64(checksum_sha256_b64)
            or not _valid_ttl(expires_in_seconds, maximum=900)
        ):
            raise ArtifactAuthorizationError

        failed = False
        url: object = None
        try:
            url = await asyncio.to_thread(
                self._client.generate_presigned_url,
                ClientMethod="put_object",
                Params={
                    "Bucket": location.bucket,
                    "Key": location.key,
                    "ContentType": content_type,
                    "ChecksumSHA256": checksum_sha256_b64,
                },
                ExpiresIn=expires_in_seconds,
                HttpMethod="PUT",
            )
        except Exception:
            failed = True
        if failed or not _is_nonempty_text(url):
            raise ArtifactAuthorizationError

        return PutAuthorization(
            location=location,
            url=url,
            required_headers=MappingProxyType(
                {
                    "Content-Type": content_type,
                    "x-amz-checksum-sha256": checksum_sha256_b64,
                }
            ),
            expires_in_seconds=expires_in_seconds,
        )

    async def create_multipart(
        self,
        *,
        location: ObjectLocation,
        content_type: str,
    ) -> MultipartCreation:
        if (
            not self._valid_location(location)
            or location.version_id is not None
            or not _is_nonempty_text(content_type)
        ):
            raise ArtifactAuthorizationError
        failed = False
        response: object = None
        try:
            response = await asyncio.to_thread(
                self._client.create_multipart_upload,
                Bucket=location.bucket,
                Key=location.key,
                ContentType=content_type,
                ChecksumAlgorithm="SHA256",
            )
        except Exception:
            failed = True
        upload_id = response.get("UploadId") if isinstance(response, Mapping) else None
        if failed or not _is_nonempty_text(upload_id):
            raise ArtifactAuthorizationError
        return MultipartCreation(location=location, storage_upload_id=upload_id)

    async def authorize_part(
        self,
        *,
        location: ObjectLocation,
        storage_upload_id: str,
        part_number: int,
        expires_in_seconds: int = 900,
    ) -> MultipartPartAuthorization:
        if (
            not self._valid_location(location)
            or location.version_id is not None
            or not _is_nonempty_text(storage_upload_id)
            or not isinstance(part_number, int)
            or isinstance(part_number, bool)
            or not 1 <= part_number <= 10_000
            or not _valid_ttl(expires_in_seconds, maximum=900)
        ):
            raise ArtifactAuthorizationError
        failed = False
        url: object = None
        try:
            url = await asyncio.to_thread(
                self._client.generate_presigned_url,
                ClientMethod="upload_part",
                Params={
                    "Bucket": location.bucket,
                    "Key": location.key,
                    "UploadId": storage_upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires_in_seconds,
                HttpMethod="PUT",
            )
        except Exception:
            failed = True
        if failed or not _is_nonempty_text(url):
            raise ArtifactAuthorizationError
        return MultipartPartAuthorization(
            part_number=part_number,
            url=url,
            required_headers=MappingProxyType({}),
            expires_in_seconds=expires_in_seconds,
        )

    async def complete_multipart(
        self,
        *,
        location: ObjectLocation,
        storage_upload_id: str,
        parts: Sequence[MultipartPart],
    ) -> CompletedMultipart:
        if (
            not self._valid_location(location)
            or location.version_id is not None
            or not _is_nonempty_text(storage_upload_id)
            or not parts
            or len(parts) > 10_000
            or any(
                item.part_number != index
                or not _is_nonempty_text(item.etag)
                or len(item.etag) > 1024
                for index, item in enumerate(parts, start=1)
            )
        ):
            raise ArtifactAuthorizationError
        failed = False
        response: object = None
        try:
            response = await asyncio.to_thread(
                self._client.complete_multipart_upload,
                Bucket=location.bucket,
                Key=location.key,
                UploadId=storage_upload_id,
                MultipartUpload={
                    "Parts": [{"PartNumber": item.part_number, "ETag": item.etag} for item in parts]
                },
            )
        except Exception:
            failed = True
        if failed or not isinstance(response, Mapping):
            raise ArtifactStorageError
        version_id = response.get("VersionId")
        if version_id is not None and not _is_immutable_version_id(version_id):
            raise ArtifactMetadataError
        return CompletedMultipart(
            location=ObjectLocation(
                bucket=location.bucket,
                key=location.key,
                version_id=version_id,
            )
        )

    async def abort_multipart(
        self,
        *,
        location: ObjectLocation,
        storage_upload_id: str,
    ) -> None:
        if (
            not self._valid_location(location)
            or location.version_id is not None
            or not _is_nonempty_text(storage_upload_id)
        ):
            raise ArtifactAuthorizationError
        failed = False
        try:
            await asyncio.to_thread(
                self._client.abort_multipart_upload,
                Bucket=location.bucket,
                Key=location.key,
                UploadId=storage_upload_id,
            )
        except Exception as error:
            if _error_code(error) == "NoSuchUpload":
                return
            failed = True
        if failed:
            raise ArtifactStorageError

    async def head(self, *, location: ObjectLocation) -> StoredObjectMetadata:
        if not self._valid_location(location) or (
            location.version_id is not None and not _is_immutable_version_id(location.version_id)
        ):
            raise ArtifactMetadataError

        request: dict[str, object] = {
            "Bucket": location.bucket,
            "Key": location.key,
        }
        if location.version_id is not None:
            request["VersionId"] = location.version_id
        request["ChecksumMode"] = "ENABLED"

        failure_code: str | None = None
        failed = False
        response: object = None
        try:
            response = await asyncio.to_thread(self._client.head_object, **request)
        except Exception as error:
            failed = True
            failure_code = _error_code(error)
        if failed:
            if failure_code in _NOT_FOUND_CODES:
                raise ArtifactNotFoundError
            raise ArtifactStorageError

        if not isinstance(response, Mapping):
            raise ArtifactMetadataError
        checksum = response.get("ChecksumSHA256")
        version_id = response.get("VersionId")
        content_type = response.get("ContentType")
        size_bytes = response.get("ContentLength")
        delete_marker = response.get("DeleteMarker", False)
        if (
            not _is_sha256_b64(checksum)
            or not _is_immutable_version_id(version_id)
            or not _is_nonempty_text(content_type)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or delete_marker is not False
            or (location.version_id is not None and version_id != location.version_id)
        ):
            raise ArtifactMetadataError

        return StoredObjectMetadata(
            location=ObjectLocation(
                bucket=location.bucket,
                key=location.key,
                version_id=version_id,
            ),
            checksum_sha256_b64=checksum,
            content_type=content_type,
            size_bytes=size_bytes,
        )

    async def authorize_get(
        self,
        *,
        location: ObjectLocation,
        expires_in_seconds: int = 300,
    ) -> GetAuthorization:
        if (
            not self._valid_location(location)
            or not _is_immutable_version_id(location.version_id)
            or not _valid_ttl(expires_in_seconds, maximum=300)
        ):
            raise ArtifactAuthorizationError

        failed = False
        url: object = None
        try:
            url = await asyncio.to_thread(
                self._client.generate_presigned_url,
                ClientMethod="get_object",
                Params={
                    "Bucket": location.bucket,
                    "Key": location.key,
                    "VersionId": location.version_id,
                    "ResponseContentDisposition": "attachment",
                    "ResponseContentType": "application/octet-stream",
                    "ResponseCacheControl": "private, no-store",
                },
                ExpiresIn=expires_in_seconds,
                HttpMethod="GET",
            )
        except Exception:
            failed = True
        if failed or not _is_nonempty_text(url):
            raise ArtifactAuthorizationError

        return GetAuthorization(
            location=location,
            url=url,
            expires_in_seconds=expires_in_seconds,
        )
