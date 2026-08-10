from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_ACCESS_TOKEN_PATTERN = re.compile(r"^ppat_[A-Za-z0-9_-]{43}$")
_REFRESH_TOKEN_PATTERN = re.compile(r"^pprt_[A-Za-z0-9_-]{43}$")
_KID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAXIMUM_CREDENTIAL_BYTES = 64 * 1024


class CredentialStoreError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent credentials are unavailable")


class CredentialBackendError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent credential backend failed")


def _decode_raw_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("key encoding is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("key encoding is invalid")
    return decoded


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("credential timestamp must include a timezone")
    return value


class TaskSigningKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kid: str
    public_key_b64: str = Field(repr=False)

    @field_validator("kid")
    @classmethod
    def validate_kid(cls, value: str) -> str:
        if _KID_PATTERN.fullmatch(value) is None:
            raise ValueError("task-signing key identifier is invalid")
        return value

    @field_validator("public_key_b64")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        _decode_raw_key(value)
        return value


class AgentCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "1.1"]
    agent_id: UUID
    team_id: UUID | None = None
    private_key_b64: str = Field(repr=False)
    access_token: str = Field(repr=False)
    access_token_expires_at: datetime
    refresh_token: str = Field(repr=False)
    refresh_token_expires_at: datetime
    task_signing_key: TaskSigningKey
    heartbeat_interval_seconds: Literal[10]

    @field_validator("private_key_b64")
    @classmethod
    def validate_private_key(cls, value: str) -> str:
        _decode_raw_key(value)
        return value

    @field_validator("access_token")
    @classmethod
    def validate_access_token(cls, value: str) -> str:
        if _ACCESS_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("access token is invalid")
        return value

    @field_validator("refresh_token")
    @classmethod
    def validate_refresh_token(cls, value: str) -> str:
        if _REFRESH_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("refresh token is invalid")
        return value

    @field_validator("access_token_expires_at", "refresh_token_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_expirations(self) -> Self:
        if self.refresh_token_expires_at <= self.access_token_expires_at:
            raise ValueError("credential expirations are invalid")
        if (self.schema_version == "1.1") != (self.team_id is not None):
            raise ValueError("credential team binding is invalid")
        return self


class CredentialBackend(Protocol):
    def read(self) -> bytes | None: ...

    def write(self, payload: bytes) -> None: ...

    def delete(self) -> None: ...


class InMemoryCredentialBackend:
    def __init__(self, value: bytes | None = None) -> None:
        self.value = value

    def read(self) -> bytes | None:
        return self.value

    def write(self, payload: bytes) -> None:
        self.value = bytes(payload)

    def delete(self) -> None:
        self.value = None


class CredentialStore:
    def __init__(self, backend: CredentialBackend) -> None:
        self._backend = backend

    def load(self) -> AgentCredentials | None:
        try:
            payload = self._backend.read()
            if payload is None:
                return None
            if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
                raise CredentialStoreError
            return AgentCredentials.model_validate_json(payload)
        except CredentialStoreError:
            raise
        except (CredentialBackendError, ValidationError, ValueError, TypeError, UnicodeError):
            raise CredentialStoreError from None

    def save(self, credentials: AgentCredentials) -> None:
        try:
            normalized = AgentCredentials.model_validate(credentials)
            payload = normalized.model_dump_json().encode("utf-8")
            if len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
                raise CredentialStoreError
            self._backend.write(payload)
        except CredentialStoreError:
            raise
        except (CredentialBackendError, ValidationError, ValueError, TypeError, UnicodeError):
            raise CredentialStoreError from None

    def delete(self) -> None:
        try:
            self._backend.delete()
        except CredentialBackendError:
            raise CredentialStoreError from None


__all__ = [
    "AgentCredentials",
    "CredentialBackend",
    "CredentialBackendError",
    "CredentialStore",
    "CredentialStoreError",
    "InMemoryCredentialBackend",
    "TaskSigningKey",
]
