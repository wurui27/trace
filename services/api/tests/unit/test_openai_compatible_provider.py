from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from perfpilot_api.ai.prompt import load_synthesis_prompt
from perfpilot_api.reports.projection import AIProjection


def _projection() -> AIProjection:
    return AIProjection(canonical_bytes=b'{"schema_version":"1.0"}', sha256_b64="checksum")


def _response(content: str = '{"schema_version":"1.0"}') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


@pytest.mark.asyncio
async def test_provider_posts_exact_bounded_chat_completion_request() -> None:
    from perfpilot_api.ai.openai_compatible import (  # type: ignore[import-not-found]
        OpenAICompatibleSynthesisProvider,
        SYNTHESIS_SCHEMA,
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response()

    prompt = load_synthesis_prompt()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        provider = OpenAICompatibleSynthesisProvider(
            base_url=SecretStr("https://provider.example.com/openai/v1/"),
            model="provider-model-1",
            token=SecretStr("private-provider-token"),
            prompt=prompt,
            max_response_bytes=128 * 1024,
            client=client,
        )
        result = await provider.synthesize(_projection())

    assert result.candidate_json == b'{"schema_version":"1.0"}'
    assert (result.prompt_tokens, result.completion_tokens) == (7, 3)
    assert result.latency_ms >= 0
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://provider.example.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer private-provider-token"
    assert json.loads(request.content) == {
        "model": "provider-model-1",
        "stream": False,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt.system_instruction},
            {"role": "user", "content": _projection().canonical_bytes.decode("utf-8")},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "perfpilot_synthesis_1_0",
                "strict": True,
                "schema": SYNTHESIS_SCHEMA,
            },
        },
    }
    assert "private-provider-token" not in repr(provider)
    assert all(
        forbidden not in json.loads(request.content)
        for forbidden in ("tools", "functions", "files", "urls", "mcp")
    )


@pytest.mark.asyncio
async def test_invalid_output_retry_sends_only_the_stable_code_not_prior_candidate() -> None:
    from perfpilot_api.ai.openai_compatible import OpenAICompatibleSynthesisProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        provider = OpenAICompatibleSynthesisProvider(
            base_url=SecretStr("https://provider.example.com/openai/v1/"),
            model="provider-model-1",
            token=SecretStr("private-provider-token"),
            prompt=load_synthesis_prompt(),
            max_response_bytes=128 * 1024,
            client=client,
        )
        await provider.synthesize(_projection(), retry_code="ai_output_invalid")

    body = json.loads(requests[0].content)
    assert body["messages"][-1] == {
        "role": "user",
        "content": "Previous output was rejected: ai_output_invalid.",
    }
    assert "candidate" not in json.dumps(body).casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(429), "ai_rate_limited", True),
        (httpx.Response(500), "ai_provider_unavailable", True),
        (httpx.Response(401), "ai_authentication_failed", False),
        (httpx.Response(302, headers={"location": "https://elsewhere.example"}), "ai_protocol_invalid", False),
        (httpx.Response(200, content=b"\xff"), "ai_protocol_invalid", False),
        (httpx.Response(200, json={"choices": []}), "ai_protocol_invalid", False),
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "{}", "refusal": "no"}}
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                },
            ),
            "ai_protocol_invalid",
            False,
        ),
    ],
)
async def test_provider_maps_remote_and_envelope_failures_to_stable_errors(
    response: httpx.Response,
    code: str,
    retryable: bool,
) -> None:
    from perfpilot_api.ai.openai_compatible import (  # type: ignore[import-not-found]
        AIProviderError,
        OpenAICompatibleSynthesisProvider,
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response), follow_redirects=False
    ) as client:
        provider = OpenAICompatibleSynthesisProvider(
            base_url=SecretStr("https://provider.example.com/v1/"),
            model="provider-model-1",
            token=SecretStr("private-provider-token"),
            prompt=load_synthesis_prompt(),
            max_response_bytes=128 * 1024,
            client=client,
        )
        with pytest.raises(AIProviderError) as exc_info:
            await provider.synthesize(_projection())

    assert (exc_info.value.stable_code, exc_info.value.retryable) == (code, retryable)
    assert "private-provider-token" not in str(exc_info.value)
    assert "private-provider-token" not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_provider_maps_timeouts_and_oversized_responses_without_disclosure() -> None:
    from perfpilot_api.ai.openai_compatible import (  # type: ignore[import-not-found]
        AIProviderError,
        OpenAICompatibleSynthesisProvider,
    )

    responses: list[httpx.Response | Exception] = [
        httpx.ReadTimeout("private-provider-body"),
        httpx.Response(200, content=b"x" * 17),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        provider = OpenAICompatibleSynthesisProvider(
            base_url=SecretStr("https://provider.example.com/v1/"),
            model="provider-model-1",
            token=SecretStr("private-provider-token"),
            prompt=load_synthesis_prompt(),
            max_response_bytes=16,
            client=client,
        )
        for code, retryable in (("ai_timeout", True), ("ai_protocol_invalid", False)):
            with pytest.raises(AIProviderError) as exc_info:
                await provider.synthesize(_projection())
            assert (exc_info.value.stable_code, exc_info.value.retryable) == (code, retryable)
            assert "private-provider" not in str(exc_info.value)
