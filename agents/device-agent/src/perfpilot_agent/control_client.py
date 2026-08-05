from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from typing import Literal, Mapping, Self
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from perfpilot_agent.config import AgentConfig
from perfpilot_agent.platform.base import AgentPlatform

_MAXIMUM_RESPONSE_BYTES = 64 * 1024
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_AGENT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class ControlClientError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent control request failed")


def _validate_raw_public_key(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("public key is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("public key is invalid")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    registration_code: str = Field(pattern=r"^ppreg_[A-Za-z0-9_-]{43}$", repr=False)
    public_key_b64: str = Field(repr=False)
    platform: AgentPlatform
    agent_version: str
    hostname: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    os_version: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("public_key_b64")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return _validate_raw_public_key(value)

    @field_validator("agent_version")
    @classmethod
    def validate_agent_version(cls, value: str) -> str:
        if len(value) > 64 or _AGENT_VERSION.fullmatch(value) is None:
            raise ValueError("Agent version is invalid")
        return value


class TaskSigningKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kid: str
    public_key_b64: str = Field(repr=False)

    @field_validator("kid")
    @classmethod
    def validate_kid(cls, value: str) -> str:
        if _KEY_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("task-signing key identifier is invalid")
        return value

    @field_validator("public_key_b64")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return _validate_raw_public_key(value)


class RegistrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    agent_id: UUID
    access_token: str = Field(pattern=r"^ppat_[A-Za-z0-9_-]{43}$", repr=False)
    access_token_expires_at: datetime
    refresh_token: str = Field(pattern=r"^pprt_[A-Za-z0-9_-]{43}$", repr=False)
    refresh_token_expires_at: datetime
    task_signing_key: TaskSigningKeyResponse
    heartbeat_interval_seconds: Literal[10]

    @field_validator("access_token_expires_at", "refresh_token_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_expirations(self) -> Self:
        if self.refresh_token_expires_at <= self.access_token_expires_at:
            raise ValueError("credential expirations are invalid")
        return self


class ControlClient:
    def __init__(
        self,
        config: AgentConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            verify=str(config.ca_bundle),
            timeout=httpx.Timeout(25.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={"accept": "application/json"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def register(
        self,
        request: RegistrationRequest | Mapping[str, object],
    ) -> RegistrationResponse:
        try:
            normalized = RegistrationRequest.model_validate(request)
            response = await self._client.post(
                f"{self._config.server_url}/v1/agent/register",
                json=normalized.model_dump(mode="json"),
            )
            if response.status_code != 201:
                raise ControlClientError
            payload = response.content
            if not payload or len(payload) > _MAXIMUM_RESPONSE_BYTES:
                raise ControlClientError
            return RegistrationResponse.model_validate_json(payload)
        except ControlClientError:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, TypeError, UnicodeError):
            raise ControlClientError from None


__all__ = [
    "ControlClient",
    "ControlClientError",
    "RegistrationRequest",
    "RegistrationResponse",
    "TaskSigningKeyResponse",
]
