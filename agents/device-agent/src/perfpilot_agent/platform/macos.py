from __future__ import annotations

import base64
import binascii
import os
import subprocess
from collections.abc import Callable, Sequence

from perfpilot_agent.credentials import CredentialBackendError

_SECURITY = "/usr/bin/security"
_SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
_SERVICE = "com.perfpilot.agent"
_ACCOUNT = "credentials"
_MAXIMUM_CREDENTIAL_BYTES = 64 * 1024

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class MacOSKeychainCredentialBackend:
    def __init__(
        self,
        runner: Runner = subprocess.run,
        *,
        effective_user_id: int | None = None,
    ) -> None:
        self._runner = runner
        self._effective_user_id = os.geteuid() if effective_user_id is None else effective_user_id

    def _keychain_arguments(self) -> list[str]:
        if self._effective_user_id == 0:
            return [_SYSTEM_KEYCHAIN]
        return []

    def _run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._runner(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise CredentialBackendError from None

    def read(self) -> bytes | None:
        result = self._run(
            [
                _SECURITY,
                "find-generic-password",
                "-s",
                _SERVICE,
                "-a",
                _ACCOUNT,
                "-w",
                *self._keychain_arguments(),
            ]
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise CredentialBackendError
        try:
            encoded = result.stdout.strip()
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise CredentialBackendError from None
        if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
            raise CredentialBackendError
        return payload

    def write(self, payload: bytes) -> None:
        if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
            raise CredentialBackendError
        encoded = base64.b64encode(payload).decode("ascii")
        result = self._run(
            [
                _SECURITY,
                "add-generic-password",
                "-U",
                "-s",
                _SERVICE,
                "-a",
                _ACCOUNT,
                "-w",
                encoded,
                *self._keychain_arguments(),
            ]
        )
        if result.returncode != 0:
            raise CredentialBackendError

    def delete(self) -> None:
        result = self._run(
            [
                _SECURITY,
                "delete-generic-password",
                "-s",
                _SERVICE,
                "-a",
                _ACCOUNT,
                *self._keychain_arguments(),
            ]
        )
        if result.returncode not in (0, 44):
            raise CredentialBackendError


__all__ = ["MacOSKeychainCredentialBackend"]
