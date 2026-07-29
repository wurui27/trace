"""Secret-safe HTTP transport shared by SmartPerfetto adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from perfpilot_api.engines.errors import EngineAdapterError


CredentialResolver = Callable[[SecretStr], Awaitable[SecretStr]]
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


@dataclass(frozen=True, slots=True)
class SmartPerfettoJsonResponse:
    status_code: int
    payload: dict[str, Any]


def _error(
    stable_code: str,
    *,
    retryable: bool,
) -> EngineAdapterError:
    return EngineAdapterError(
        stable_code=stable_code,
        retryable=retryable,
        terminal_state=None if retryable else "failed",
    )


def validate_external_id(value: str) -> str:
    if value in {".", ".."} or _EXTERNAL_ID.fullmatch(value) is None:
        raise _error("engine_contract_invalid", retryable=False)
    return value


def _validate_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise _error("engine_contract_invalid", retryable=False) from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise _error("engine_contract_invalid", retryable=False)
    return value.rstrip("/")


def _validate_path(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "://" in value
        or "?" in value
        or "#" in value
        or "\\" in value
    ):
        raise _error("engine_contract_invalid", retryable=False)
    return value


class SmartPerfettoTransport:
    def __init__(
        self,
        *,
        base_url: str,
        credential_reference: SecretStr,
        credential_resolver: CredentialResolver,
        max_json_bytes: int,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        if max_json_bytes <= 0:
            raise ValueError("max_json_bytes must be positive")
        if client is not None and client.follow_redirects:
            raise ValueError("SmartPerfetto client must not follow redirects")
        self._base_url = _validate_base_url(base_url)
        self._credential_reference = credential_reference
        self._credential_resolver = credential_resolver
        self._max_json_bytes = max_json_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout
            or httpx.Timeout(
                timeout=30.0,
                connect=5.0,
                read=30.0,
                write=30.0,
                pool=5.0,
            ),
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        workspace_id: str | None = None,
        json_body: dict[str, object] | None = None,
    ) -> SmartPerfettoJsonResponse:
        safe_path = _validate_path(path)
        headers = {"Accept": "application/json"}
        if workspace_id is not None:
            headers["X-Workspace-Id"] = validate_external_id(workspace_id)

        try:
            resolved = await self._credential_resolver(self._credential_reference)
            if not isinstance(resolved, SecretStr):
                raise TypeError
            token = resolved.get_secret_value()
            if not token.strip():
                raise ValueError
        except Exception:
            raise _error("engine_auth_failed", retryable=False) from None
        headers["Authorization"] = f"Bearer {token}"

        response: httpx.Response | None = None
        try:
            request = self._client.build_request(
                method.upper(),
                f"{self._base_url}{safe_path}",
                headers=headers,
                json=json_body,
            )
            response = await self._client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if response.status_code in {401, 403}:
                raise _error("engine_auth_failed", retryable=False)
            if 500 <= response.status_code <= 599:
                raise _error("engine_unavailable", retryable=True)
            if 300 <= response.status_code <= 399:
                raise _error("engine_contract_invalid", retryable=False)

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_json_bytes:
                    raise _error("engine_contract_invalid", retryable=False)
                chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks))
            except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
                raise _error("engine_contract_invalid", retryable=False) from None
            if not isinstance(payload, dict):
                raise _error("engine_contract_invalid", retryable=False)
            return SmartPerfettoJsonResponse(
                status_code=response.status_code,
                payload=payload,
            )
        except EngineAdapterError:
            raise
        except httpx.TimeoutException:
            raise _error("engine_timeout", retryable=True) from None
        except httpx.RequestError:
            raise _error("engine_unavailable", retryable=True) from None
        finally:
            if response is not None:
                await response.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "CredentialResolver",
    "SmartPerfettoJsonResponse",
    "SmartPerfettoTransport",
    "validate_external_id",
]
