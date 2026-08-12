from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.smartperfetto_transport import (
    SmartPerfettoJsonResponse,
    SmartPerfettoTransport,
    validate_external_id,
)


def test_engine_adapter_error_has_stable_redacted_semantics() -> None:
    error = EngineAdapterError(
        stable_code="engine_unavailable",
        retryable=True,
        terminal_state=None,
    )

    assert error.stable_code == "engine_unavailable"
    assert error.retryable is True
    assert error.terminal_state is None
    assert str(error) == "engine adapter operation failed"
    assert repr(error) == "EngineAdapterError(<redacted>)"


@pytest.mark.parametrize(
    "value",
    ["", ".", "../escape", "with/slash", "with?query", "white space", "ü", "a" * 256],
)
def test_external_ids_are_validated_before_url_interpolation(value: str) -> None:
    with pytest.raises(EngineAdapterError) as exc_info:
        validate_external_id(value)
    assert exc_info.value.stable_code == "engine_contract_invalid"
    if value:
        assert value not in str(exc_info.value)


@pytest.mark.asyncio
async def test_transport_sends_only_server_owned_auth_and_workspace_headers() -> None:
    requests: list[httpx.Request] = []
    credential_reference = SecretStr("vault://smartperfetto/service")

    async def resolve(reference: SecretStr) -> SecretStr:
        assert reference.get_secret_value() == credential_reference.get_secret_value()
        return SecretStr("service-api-key-secret-marker")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    transport = SmartPerfettoTransport(
        base_url="https://smartperfetto.example.com",
        credential_reference=credential_reference,
        credential_resolver=resolve,
        client=client,
        max_json_bytes=4096,
    )
    try:
        response = await transport.request_json(
            "GET",
            "/api/tenant/workspaces",
            workspace_id="workspace-server-owned",
        )
    finally:
        await transport.aclose()

    assert response == SmartPerfettoJsonResponse(
        status_code=200,
        payload={"success": True},
        raw_body=b'{"success":true}',
    )
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer service-api-key-secret-marker"
    assert requests[0].headers["Accept"] == "application/json"
    assert requests[0].headers["X-Workspace-Id"] == "workspace-server-owned"
    assert client.is_closed is False
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_rejects_redirects_and_never_follows_them() -> None:
    requests: list[httpx.Request] = []

    async def resolve(_: SecretStr) -> SecretStr:
        return SecretStr("service-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.invalid/private"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    transport = SmartPerfettoTransport(
        base_url="https://smartperfetto.example.com",
        credential_reference=SecretStr("secret-ref"),
        credential_resolver=resolve,
        client=client,
        max_json_bytes=4096,
    )
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await transport.request_json("GET", "/api/tenant/workspaces")
    finally:
        await client.aclose()

    assert exc_info.value.stable_code == "engine_contract_invalid"
    assert len(requests) == 1
    assert "evil.invalid" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("status_code", "stable_code", "retryable"),
    [
        (401, "engine_auth_failed", False),
        (403, "engine_auth_failed", False),
        (500, "engine_unavailable", True),
        (503, "engine_unavailable", True),
    ],
)
@pytest.mark.asyncio
async def test_transport_maps_auth_and_server_errors_without_response_text(
    status_code: int,
    stable_code: str,
    retryable: bool,
) -> None:
    marker = "response-secret-marker"

    async def resolve(_: SecretStr) -> SecretStr:
        return SecretStr("service-secret")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=marker)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        transport = SmartPerfettoTransport(
            base_url="https://smartperfetto.example.com",
            credential_reference=SecretStr("secret-ref"),
            credential_resolver=resolve,
            client=client,
            max_json_bytes=4096,
        )
        with pytest.raises(EngineAdapterError) as exc_info:
            await transport.request_json("GET", "/api/tenant/workspaces")

    assert exc_info.value.stable_code == stable_code
    assert exc_info.value.retryable is retryable
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_transport_maps_connectivity_and_timeout_failures() -> None:
    async def resolve(_: SecretStr) -> SecretStr:
        return SecretStr("service-secret")

    for raised, stable_code in (
        (httpx.ConnectError("url-secret-marker"), "engine_unavailable"),
        (httpx.ReadTimeout("url-secret-marker"), "engine_timeout"),
    ):
        def handler(request: httpx.Request, error: Exception = raised) -> httpx.Response:
            raise error

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            transport = SmartPerfettoTransport(
                base_url="https://smartperfetto.example.com",
                credential_reference=SecretStr("secret-ref"),
                credential_resolver=resolve,
                client=client,
                max_json_bytes=4096,
            )
            with pytest.raises(EngineAdapterError) as exc_info:
                await transport.request_json("GET", "/api/tenant/workspaces")
        assert exc_info.value.stable_code == stable_code
        assert exc_info.value.retryable is True
        assert "url-secret-marker" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_transport_bounds_and_parses_json_exactly_once() -> None:
    async def resolve(_: SecretStr) -> SecretStr:
        return SecretStr("service-secret")

    responses = [
        httpx.Response(200, content=b'{"success":true}'),
        httpx.Response(200, content=b"{" + b"x" * 32),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        transport = SmartPerfettoTransport(
            base_url="https://smartperfetto.example.com",
            credential_reference=SecretStr("secret-ref"),
            credential_resolver=resolve,
            client=client,
            max_json_bytes=24,
        )
        response = await transport.request_json("GET", "/api/tenant/workspaces")
        assert response.payload == {"success": True}
        with pytest.raises(EngineAdapterError) as exc_info:
            await transport.request_json("GET", "/api/tenant/workspaces")
    assert exc_info.value.stable_code == "engine_contract_invalid"


@pytest.mark.asyncio
async def test_transport_rejects_malformed_or_non_object_json() -> None:
    async def resolve(_: SecretStr) -> SecretStr:
        return SecretStr("service-secret")

    responses = [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, content=json.dumps(["not", "an", "object"]).encode()),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        transport = SmartPerfettoTransport(
            base_url="https://smartperfetto.example.com",
            credential_reference=SecretStr("secret-ref"),
            credential_resolver=resolve,
            client=client,
            max_json_bytes=4096,
        )
        for _ in range(2):
            with pytest.raises(EngineAdapterError) as exc_info:
                await transport.request_json("GET", "/api/tenant/workspaces")
            assert exc_info.value.stable_code == "engine_contract_invalid"


@pytest.mark.asyncio
async def test_transport_closes_only_a_client_it_owns() -> None:
    async def resolve(_: SecretStr) -> SecretStr:
        return SecretStr("service-secret")

    transport = SmartPerfettoTransport(
        base_url="https://smartperfetto.example.com",
        credential_reference=SecretStr("secret-ref"),
        credential_resolver=resolve,
        max_json_bytes=4096,
    )
    owned_client = transport.client

    await transport.aclose()

    assert owned_client.is_closed is True
