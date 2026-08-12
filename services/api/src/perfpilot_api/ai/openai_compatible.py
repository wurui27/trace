"""A narrow, secret-safe OpenAI-compatible synthesis client."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from perfpilot_api.ai.prompt import SynthesisPrompt
from perfpilot_api.reports.projection import AIProjection


def _load_synthesis_schema() -> dict[str, object]:
    try:
        schema_path = Path(__file__).resolve().parents[5] / "contracts/v1/ai/synthesis-output.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("synthesis schema is unavailable") from None
    if not isinstance(schema, dict):
        raise RuntimeError("synthesis schema is unavailable")
    return schema


SYNTHESIS_SCHEMA = _load_synthesis_schema()


class AIProviderError(RuntimeError):
    def __init__(
        self,
        stable_code: str,
        *,
        retryable: bool,
        detail_code: str = "unspecified",
    ) -> None:
        self.stable_code = stable_code
        self.retryable = retryable
        self.detail_code = detail_code
        super().__init__(stable_code)


@dataclass(frozen=True, slots=True)
class SynthesisCandidate:
    candidate_json: bytes = field(repr=False)
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


def _error(
    stable_code: str,
    *,
    retryable: bool,
    detail_code: str = "unspecified",
) -> AIProviderError:
    return AIProviderError(
        stable_code,
        retryable=retryable,
        detail_code=detail_code,
    )


def _validated_base_url(value: SecretStr) -> str:
    try:
        raw_url = value.get_secret_value()
        parsed_url = urlsplit(raw_url)
    except ValueError:
        raise ValueError("AI provider URL is invalid") from None
    if (
        parsed_url.scheme.casefold() not in {"http", "https"}
        or parsed_url.hostname is None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or not parsed_url.path.endswith("/v1/")
        or "\\" in parsed_url.path
    ):
        raise ValueError("AI provider URL is invalid")
    return raw_url.rstrip("/")


def _nonnegative_int(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


class OpenAICompatibleSynthesisProvider:
    def __init__(
        self,
        *,
        base_url: SecretStr,
        model: str,
        token: SecretStr,
        prompt: SynthesisPrompt,
        max_response_bytes: int,
        response_format: Literal["json_schema", "json_object"] = "json_schema",
        max_completion_tokens: int | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        if not isinstance(model, str) or not model or len(model) > 128:
            raise ValueError("AI provider model is invalid")
        if not isinstance(prompt, SynthesisPrompt):
            raise TypeError("prompt must be a SynthesisPrompt")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if response_format not in {"json_schema", "json_object"}:
            raise ValueError("AI provider response format is invalid")
        if max_completion_tokens is not None and (
            type(max_completion_tokens) is not int
            or not 1 <= max_completion_tokens <= 65_536
        ):
            raise ValueError("AI provider token limit is invalid")
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("AI provider thinking mode is invalid")
        if not token.get_secret_value().strip():
            raise ValueError("AI provider token is invalid")
        if client is not None and client.follow_redirects:
            raise ValueError("AI provider client must not follow redirects")
        self._base_url = _validated_base_url(base_url)
        self._model = model
        self._token = token
        self._prompt = prompt
        self._max_response_bytes = max_response_bytes
        self._response_format = response_format
        self._max_completion_tokens = max_completion_tokens
        self._thinking_mode = thinking_mode
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout
            or httpx.Timeout(timeout=60.0, connect=5.0, read=60.0, write=30.0, pool=5.0),
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _bounded_body(self, response: httpx.Response) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = self._max_response_bytes + 1 - len(body)
            body.extend(chunk[:remaining])
            if len(chunk) > remaining or len(body) > self._max_response_bytes:
                raise _error(
                    "ai_protocol_invalid",
                    retryable=False,
                    detail_code="response_too_large",
                )
        return bytes(body)

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if 300 <= response.status_code <= 399:
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="http_redirect",
            )
        if response.status_code == 429:
            raise _error(
                "ai_rate_limited", retryable=True, detail_code="http_rate_limited"
            )
        if 500 <= response.status_code <= 599:
            raise _error(
                "ai_provider_unavailable",
                retryable=True,
                detail_code="http_server_error",
            )
        if response.status_code in {401, 403}:
            raise _error(
                "ai_authentication_failed",
                retryable=False,
                detail_code="http_authentication",
            )
        if response.status_code != 200:
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="http_unexpected_status",
            )

    @staticmethod
    def _candidate_from_response(payload: object, *, latency_ms: int) -> SynthesisCandidate:
        if not isinstance(payload, dict):
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="response_not_object",
            )
        choices = payload.get("choices")
        usage = payload.get("usage")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(usage, dict):
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="choices_or_usage_invalid",
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="choice_invalid",
            )
        if choice.get("finish_reason") != "stop":
            reason = choice.get("finish_reason")
            safe_reason = reason if isinstance(reason, str) and reason.isidentifier() else "invalid"
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code=f"finish_reason_{safe_reason}",
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="message_invalid",
            )
        content = message.get("content")
        if (
            not isinstance(content, str)
            or message.get("refusal") is not None
            or message.get("tool_calls") is not None
            or message.get("function_call") is not None
        ):
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="message_content_invalid",
            )
        prompt_tokens = _nonnegative_int(usage.get("prompt_tokens"))
        completion_tokens = _nonnegative_int(usage.get("completion_tokens"))
        if prompt_tokens is None or completion_tokens is None or latency_ms < 0:
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="usage_invalid",
            )
        try:
            candidate_json = content.encode("utf-8")
        except UnicodeError:
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="content_encoding_invalid",
            ) from None
        return SynthesisCandidate(
            candidate_json=candidate_json,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )

    async def synthesize(
        self,
        projection: AIProjection,
        *,
        retry_code: str | None = None,
    ) -> SynthesisCandidate:
        if not isinstance(projection, AIProjection):
            raise TypeError("projection must be an AIProjection")
        if retry_code not in {
            None,
            "ai_output_invalid",
            "ai_narrative_language_invalid",
        }:
            raise ValueError("AI retry context is invalid")
        try:
            projection_text = projection.canonical_bytes.decode("utf-8")
        except UnicodeError:
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="projection_encoding_invalid",
            ) from None
        messages = [
            {"role": "system", "content": self._prompt.system_instruction},
            {"role": "user", "content": projection_text},
        ]
        if retry_code is not None:
            # The rejected candidate is deliberately not retained or reflected.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "上一候选的面向用户叙述不是简体中文；只修正叙述语言，保持证据、ID、数值、代码和 Diff 不变。"
                        if retry_code == "ai_narrative_language_invalid"
                        else (
                            "The previous output failed validation. Generate a complete "
                            "new JSON object from the authoritative projection. Strictly "
                            "check the schema and every referenced identifier. Narrative "
                            "fields must contain no ASCII digits; measurements belong only "
                            "in report metric sections."
                        )
                    ),
                }
            )
        response_format: dict[str, object]
        if self._response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "perfpilot_synthesis_1_0",
                    "strict": True,
                    "schema": SYNTHESIS_SCHEMA,
                },
            }
        else:
            response_format = {"type": "json_object"}
        request_json = {
            "model": self._model,
            "stream": False,
            "temperature": 0,
            "messages": messages,
            "response_format": response_format,
        }
        if self._max_completion_tokens is not None:
            request_json["max_tokens"] = self._max_completion_tokens
        if self._thinking_mode is not None:
            request_json["thinking"] = {"type": self._thinking_mode}
        started = time.monotonic_ns()
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._token.get_secret_value()}",
                },
                json=request_json,
                follow_redirects=False,
            ) as response:
                self._check_status(response)
                raw_body = await self._bounded_body(response)
        except AIProviderError:
            raise
        except httpx.TimeoutException:
            raise _error(
                "ai_timeout", retryable=True, detail_code="transport_timeout"
            ) from None
        except httpx.ProtocolError:
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="transport_protocol_error",
            ) from None
        except httpx.RequestError:
            raise _error(
                "ai_provider_unavailable",
                retryable=True,
                detail_code="transport_request_error",
            ) from None
        try:
            payload: Any = json.loads(raw_body.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise _error(
                "ai_protocol_invalid",
                retryable=False,
                detail_code="response_json_invalid",
            ) from None
        latency_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        return self._candidate_from_response(payload, latency_ms=latency_ms)


__all__ = [
    "AIProviderError",
    "OpenAICompatibleSynthesisProvider",
    "SYNTHESIS_SCHEMA",
    "SynthesisCandidate",
]
