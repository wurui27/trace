from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from perfpilot_agent import __version__
from perfpilot_agent.control_client import RegistrationRequest, RegistrationResponse
from perfpilot_agent.credentials import AgentCredentials, CredentialStore, TaskSigningKey
from perfpilot_agent.platform.base import PlatformMetadata

_REGISTRATION_CODE = re.compile(rb"^ppreg_[A-Za-z0-9_-]{43}$")


class RegistrationError(RuntimeError):
    def __init__(self, message: str = "PerfPilot Agent registration failed") -> None:
        super().__init__(message)


class RegistrationAlreadyExists(RegistrationError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent is already registered")


class ReplacementConfirmationRequired(RegistrationError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent replacement requires local confirmation")


class RegistrationClient(Protocol):
    async def register(self, request: RegistrationRequest) -> RegistrationResponse: ...


class RegistrationService:
    def __init__(
        self,
        *,
        store: CredentialStore,
        client: RegistrationClient,
        metadata: PlatformMetadata,
        private_key_factory: Callable[[], Ed25519PrivateKey] = Ed25519PrivateKey.generate,
    ) -> None:
        self._store = store
        self._client = client
        self.metadata = metadata
        self._private_key_factory = private_key_factory

    async def register(
        self,
        registration_code: bytearray,
        *,
        replace: bool = False,
        replacement_confirmed: bool = False,
    ) -> AgentCredentials:
        try:
            existing = self._store.load()
            if existing is not None and not replace:
                raise RegistrationAlreadyExists
            if existing is not None and replace and not replacement_confirmed:
                raise ReplacementConfirmationRequired
            raw_code = bytes(registration_code)
            if _REGISTRATION_CODE.fullmatch(raw_code) is None:
                raise RegistrationError
            private_key = self._private_key_factory()
            private_raw = private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            public_raw = private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            request = RegistrationRequest(
                schema_version="1.1",
                registration_code=raw_code.decode("ascii"),
                public_key_b64=base64.b64encode(public_raw).decode("ascii"),
                platform=self.metadata.platform,
                agent_version=__version__,
                hostname=self.metadata.hostname,
                os_version=self.metadata.os_version,
            )
            response = await self._client.register(request)
            credentials = AgentCredentials(
                schema_version=response.schema_version,
                agent_id=response.agent_id,
                team_id=response.team_id,
                private_key_b64=base64.b64encode(private_raw).decode("ascii"),
                access_token=response.access_token,
                access_token_expires_at=response.access_token_expires_at,
                refresh_token=response.refresh_token,
                refresh_token_expires_at=response.refresh_token_expires_at,
                task_signing_key=TaskSigningKey(
                    kid=response.task_signing_key.kid,
                    public_key_b64=response.task_signing_key.public_key_b64,
                ),
                heartbeat_interval_seconds=response.heartbeat_interval_seconds,
            )
            self._store.save(credentials)
            return credentials
        except (RegistrationAlreadyExists, ReplacementConfirmationRequired):
            raise
        except (RegistrationError, ValidationError, UnicodeError, ValueError, TypeError):
            raise RegistrationError from None
        finally:
            for index in range(len(registration_code)):
                registration_code[index] = 0


__all__ = [
    "RegistrationAlreadyExists",
    "RegistrationClient",
    "RegistrationError",
    "RegistrationService",
    "ReplacementConfirmationRequired",
]
