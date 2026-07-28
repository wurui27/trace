from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


class ArtifactStorageError(RuntimeError):
    """A redacted failure while accessing artifact storage."""

    message = "artifact storage operation failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class ArtifactAuthorizationError(ArtifactStorageError):
    message = "artifact authorization could not be created"


class ArtifactNotFoundError(ArtifactStorageError):
    message = "artifact object was not found"


class ArtifactMetadataError(ArtifactStorageError):
    message = "artifact object metadata is invalid"


@dataclass(frozen=True, slots=True)
class ObjectLocation:
    bucket: str = field(repr=False)
    key: str = field(repr=False)
    version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PutAuthorization:
    location: ObjectLocation = field(repr=False)
    url: str = field(repr=False)
    required_headers: Mapping[str, str] = field(repr=False)
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class StoredObjectMetadata:
    location: ObjectLocation = field(repr=False)
    checksum_sha256_b64: str = field(repr=False)
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class GetAuthorization:
    location: ObjectLocation = field(repr=False)
    url: str = field(repr=False)
    expires_in_seconds: int


class ArtifactStore(Protocol):
    async def authorize_put(
        self,
        *,
        location: ObjectLocation,
        content_type: str,
        checksum_sha256_b64: str,
        expires_in_seconds: int = 900,
    ) -> PutAuthorization: ...

    async def head(self, *, location: ObjectLocation) -> StoredObjectMetadata: ...

    async def authorize_get(
        self,
        *,
        location: ObjectLocation,
        expires_in_seconds: int = 300,
    ) -> GetAuthorization: ...
