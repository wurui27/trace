"""One bounded, evidence-grounded AI report pass for the local runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, Mapping, Protocol, cast, runtime_checkable

import httpx
from pydantic import SecretStr

from perfpilot_api.ai.openai_compatible import (
    AIProviderError,
    SYNTHESIS_SCHEMA,
    OpenAICompatibleSynthesisProvider,
    SynthesisCandidate,
)
from perfpilot_api.ai.prompt import SynthesisPrompt
from perfpilot_api.ai.synthesis import (
    AISynthesisOutput,
    SynthesisValidationError,
    validate_synthesis_output,
)
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.privacy import reject_private_json
from perfpilot_api.reports.projection import AIProjection


ReportRole = Literal["report"]
ReportState = Literal["running", "completed", "failed"]
OutputRetryCode = Literal["ai_output_invalid"]
ReportObserver = Callable[
    [int, ReportRole, ReportState, int, AISynthesisOutput | None],
    Awaitable[None],
]


class LocalSynthesisError(RuntimeError):
    def __init__(
        self,
        stable_code: str,
        *,
        round_number: int | None = None,
        retryable: bool = True,
        detail_code: str = "unspecified",
    ) -> None:
        self.stable_code = stable_code
        self.round_number = round_number
        self.retryable = retryable
        self.detail_code = detail_code
        super().__init__(stable_code)


class LocalReportProvider(Protocol):
    async def complete(self, *, projection: AIProjection) -> SynthesisCandidate: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class RetryAwareLocalReportProvider(Protocol):
    async def complete_retry(
        self,
        *,
        projection: AIProjection,
        retry_code: OutputRetryCode,
    ) -> SynthesisCandidate: ...


_PROMPT_RESOURCE = "perfpilot-report-v3.txt"
_MAX_PROJECTION_BYTES = 256 * 1024


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _local_prompt() -> SynthesisPrompt:
    try:
        raw = files("perfpilot_api.ai.prompts").joinpath(_PROMPT_RESOURCE).read_bytes()
        instruction = raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise RuntimeError("local AI prompt is unavailable") from None
    if not raw or len(raw) > 32 * 1024 or not instruction.strip():
        raise RuntimeError("local AI prompt is unavailable")
    return SynthesisPrompt(
        version=_PROMPT_RESOURCE.removesuffix(".txt"),
        sha256_b64=_checksum(raw),
        system_instruction=instruction,
        raw_bytes=raw,
    )


def _validated_local_projection(projection: AIProjection) -> AIProjection:
    try:
        payload = projection.canonical_bytes
        if type(payload) is not bytes or not 1 <= len(payload) <= _MAX_PROJECTION_BYTES:
            raise ValueError
        if (
            type(projection.sha256_b64) is not str
            or projection.sha256_b64 != _checksum(payload)
        ):
            raise ValueError
        document = projection.document
        reject_private_json(document)
        validated = validate_contract("analysis-projection", document)
        canonical_payload = canonical_json_bytes(validated)
        if canonical_payload != payload:
            raise ValueError
    except Exception:
        raise ValueError("local AI report input is invalid") from None
    return AIProjection(
        canonical_bytes=canonical_payload,
        sha256_b64=_checksum(canonical_payload),
    )


def build_local_report_projection(projection: AIProjection) -> AIProjection:
    if not isinstance(projection, AIProjection):
        raise ValueError("local AI report input is invalid")
    projection_document = _validated_local_projection(projection).document
    numeric_spellings: set[str] = set()
    for scenario in projection_document["scenarios"]:  # type: ignore[index]
        for metric in scenario["metrics"]:
            numeric_value = metric.get("numeric_value")
            if numeric_value is not None:
                numeric_spellings.add(
                    canonical_json_bytes(numeric_value).decode("ascii")
                )
            threshold = metric.get("threshold")
            if threshold is not None:
                numeric_spellings.add(
                    canonical_json_bytes(threshold["value"]).decode("ascii")
                )
    envelope = canonical_json_bytes(
        {
            "allowed_numeric_spellings": sorted(numeric_spellings),
            "authoritative_projection": projection_document,
            "output_schema": SYNTHESIS_SCHEMA,
            "round_role": "report",
        }
    )
    return AIProjection(
        canonical_bytes=envelope,
        sha256_b64=_checksum(envelope),
    )


class LocalOpenAICompatibleReportProvider:
    prompt_version = "perfpilot-report-v3"

    def __init__(
        self,
        *,
        base_url: SecretStr,
        model: str,
        token: SecretStr,
        provider_name: str,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
    ) -> None:
        if not provider_name.strip() or len(provider_name) > 128:
            raise ValueError("local AI provider name is invalid")
        prompt = _local_prompt()
        self.provider_name = provider_name.strip()
        self.model = model
        self.prompt_sha256_b64 = prompt.sha256_b64
        self._provider = OpenAICompatibleSynthesisProvider(
            base_url=base_url,
            model=model,
            token=token,
            prompt=prompt,
            max_response_bytes=128 * 1024,
            response_format="json_object",
            max_completion_tokens=8192,
            thinking_mode=thinking_mode,
            timeout=httpx.Timeout(
                timeout=120.0,
                connect=10.0,
                read=120.0,
                write=30.0,
                pool=10.0,
            ),
        )

    async def complete(self, *, projection: AIProjection) -> SynthesisCandidate:
        provider_projection = build_local_report_projection(projection)
        return await self._provider.synthesize(provider_projection)

    async def complete_retry(
        self,
        *,
        projection: AIProjection,
        retry_code: OutputRetryCode,
    ) -> SynthesisCandidate:
        provider_projection = build_local_report_projection(projection)
        return await self._provider.synthesize(
            provider_projection,
            retry_code=retry_code,
        )

    async def aclose(self) -> None:
        await self._provider.aclose()


def build_local_report_synthesizer(
    environ: Mapping[str, str] | None = None,
) -> LocalReportSynthesizer | None:
    values = os.environ if environ is None else environ
    base_url = values.get("PERFPILOT_LOCAL_AI_BASE_URL", "").strip()
    model = values.get("PERFPILOT_LOCAL_AI_MODEL", "").strip()
    token = values.get("PERFPILOT_LOCAL_AI_TOKEN", "").strip()
    if not base_url or not model or not token:
        return None
    provider_name = values.get(
        "PERFPILOT_LOCAL_AI_PROVIDER_NAME", "openai-compatible-local"
    ).strip()
    thinking_value = values.get("PERFPILOT_LOCAL_AI_THINKING", "").strip()
    if thinking_value not in {"", "enabled", "disabled"}:
        raise ValueError("local AI thinking mode is invalid")
    thinking_mode = cast(
        Literal["enabled", "disabled"] | None,
        thinking_value or None,
    )
    provider = LocalOpenAICompatibleReportProvider(
        base_url=SecretStr(base_url),
        model=model,
        token=SecretStr(token),
        provider_name=provider_name,
        thinking_mode=thinking_mode,
    )
    return LocalReportSynthesizer(provider=provider)


@dataclass(frozen=True, slots=True)
class LocalReportUsage:
    number: Literal[1]
    role: ReportRole
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LocalSynthesisResult:
    output: AISynthesisOutput
    rounds: tuple[LocalReportUsage]


class LocalReportSynthesizer:
    def __init__(self, *, provider: LocalReportProvider) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return str(getattr(self._provider, "provider_name", "local-ai"))

    @property
    def model(self) -> str:
        return str(getattr(self._provider, "model", "configured-model"))

    @property
    def prompt_version(self) -> str:
        return str(
            getattr(
                self._provider,
                "prompt_version",
                "perfpilot-report-v3",
            )
        )

    @property
    def prompt_sha256_b64(self) -> str:
        return str(getattr(self._provider, "prompt_sha256_b64", ""))

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def synthesize(
        self,
        projection: AIProjection,
        *,
        on_report: ReportObserver | None = None,
    ) -> LocalSynthesisResult:
        if not isinstance(projection, AIProjection):
            raise TypeError("projection must be an AIProjection")
        projection = _validated_local_projection(projection)
        if on_report is not None:
            await on_report(1, "report", "running", 0, None)
        attempts = 0
        failure_code = "ai_output_invalid"
        retryable = True
        detail_code = "unspecified"
        prompt_tokens = 0
        completion_tokens = 0
        latency_ms = 0
        retry_code: OutputRetryCode | None = None
        while attempts < 2:
            attempts += 1
            try:
                if retry_code is not None and isinstance(
                    self._provider,
                    RetryAwareLocalReportProvider,
                ):
                    candidate = await self._provider.complete_retry(
                        projection=projection,
                        retry_code=retry_code,
                    )
                else:
                    candidate = await self._provider.complete(projection=projection)
                prompt_tokens += candidate.prompt_tokens
                completion_tokens += candidate.completion_tokens
                latency_ms += candidate.latency_ms
                output = validate_synthesis_output(
                    projection=projection,
                    candidate=candidate.candidate_json,
                )
                usage = LocalReportUsage(
                    number=1,
                    role="report",
                    attempts=attempts,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                )
                if on_report is not None:
                    await on_report(1, "report", "completed", attempts, output)
                return LocalSynthesisResult(output=output, rounds=(usage,))
            except SynthesisValidationError:
                failure_code = "ai_output_invalid"
                retryable = True
                detail_code = "semantic_validation"
                retry_code = "ai_output_invalid"
            except AIProviderError as error:
                failure_code = error.stable_code
                retryable = error.retryable
                detail_code = error.detail_code
                retry_code = None
                if not retryable:
                    break
            except asyncio.CancelledError:
                raise
        if on_report is not None:
            await on_report(1, "report", "failed", attempts, None)
        raise LocalSynthesisError(
            failure_code,
            round_number=1,
            retryable=retryable,
            detail_code=detail_code,
        )


__all__ = [
    "LocalOpenAICompatibleReportProvider",
    "LocalReportProvider",
    "LocalReportSynthesizer",
    "LocalReportUsage",
    "LocalSynthesisError",
    "LocalSynthesisResult",
    "OutputRetryCode",
    "ReportObserver",
    "ReportRole",
    "ReportState",
    "RetryAwareLocalReportProvider",
    "build_local_report_projection",
    "build_local_report_synthesizer",
]
