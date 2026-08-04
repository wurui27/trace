"""Three bounded, evidence-grounded AI revisions for the local runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Mapping
from typing import Literal, Protocol

import httpx
from pydantic import SecretStr

from perfpilot_api.ai.openai_compatible import (
    AIProviderError,
    OpenAICompatibleSynthesisProvider,
    SYNTHESIS_SCHEMA,
    SynthesisCandidate,
)
from perfpilot_api.ai.prompt import SynthesisPrompt
from perfpilot_api.ai.synthesis import (
    AISynthesisOutput,
    SynthesisValidationError,
    validate_synthesis_output,
)
from perfpilot_api.reports.projection import AIProjection
from perfpilot_api.reports.contracts import canonical_json_bytes


RoundRole = Literal["extract", "review", "finalize"]
RoundState = Literal["running", "completed", "failed"]
RoundObserver = Callable[
    [int, RoundRole, RoundState, AISynthesisOutput | None],
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


class LocalRoundProvider(Protocol):
    async def complete(
        self,
        *,
        role: RoundRole,
        projection: AIProjection,
        prior_outputs: Sequence[AISynthesisOutput],
    ) -> SynthesisCandidate: ...

    async def aclose(self) -> None: ...


_PROMPT_RESOURCES: dict[RoundRole, str] = {
    "extract": "perfpilot-local-extract-v1.txt",
    "review": "perfpilot-local-review-v1.txt",
    "finalize": "perfpilot-local-finalize-v1.txt",
}


def _checksum(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _local_prompts() -> dict[RoundRole, SynthesisPrompt]:
    prompts: dict[RoundRole, SynthesisPrompt] = {}
    for role, resource in _PROMPT_RESOURCES.items():
        try:
            raw = files("perfpilot_api.ai.prompts").joinpath(resource).read_bytes()
            instruction = raw.decode("utf-8")
        except (OSError, UnicodeError):
            raise RuntimeError("local AI prompts are unavailable") from None
        if not raw or len(raw) > 32 * 1024 or not instruction.strip():
            raise RuntimeError("local AI prompts are unavailable")
        prompts[role] = SynthesisPrompt(
            version=resource.removesuffix(".txt"),
            sha256_b64=_checksum(raw),
            system_instruction=instruction,
            raw_bytes=raw,
        )
    return prompts


def build_local_round_projection(
    *,
    role: RoundRole,
    projection: AIProjection,
    prior_outputs: Sequence[AISynthesisOutput],
) -> AIProjection:
    if role not in _PROMPT_RESOURCES or not isinstance(projection, AIProjection):
        raise ValueError("local AI round input is invalid")
    projection_document = projection.document
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
            "prior_validated_outputs": [item.document for item in prior_outputs],
            "round_role": role,
        }
    )
    return AIProjection(
        canonical_bytes=envelope,
        sha256_b64=_checksum(envelope),
    )


class LocalOpenAICompatibleRoundProvider:
    prompt_version = "perfpilot-local-multiround-v1"

    def __init__(
        self,
        *,
        base_url: SecretStr,
        model: str,
        token: SecretStr,
        provider_name: str,
    ) -> None:
        if not provider_name.strip() or len(provider_name) > 128:
            raise ValueError("local AI provider name is invalid")
        prompts = _local_prompts()
        self.provider_name = provider_name.strip()
        self.model = model
        combined = b"\0".join(prompts[role].raw_bytes for role in _PROMPT_RESOURCES)
        self.prompt_sha256_b64 = _checksum(combined)
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(
                timeout=120.0,
                connect=10.0,
                read=120.0,
                write=30.0,
                pool=10.0,
            ),
        )
        self._providers = {
            role: OpenAICompatibleSynthesisProvider(
                base_url=base_url,
                model=model,
                token=token,
                prompt=prompt,
                max_response_bytes=128 * 1024,
                response_format="json_object",
                max_completion_tokens=8192,
                client=self._client,
            )
            for role, prompt in prompts.items()
        }

    async def complete(
        self,
        *,
        role: RoundRole,
        projection: AIProjection,
        prior_outputs: Sequence[AISynthesisOutput],
    ) -> SynthesisCandidate:
        provider_projection = build_local_round_projection(
            role=role,
            projection=projection,
            prior_outputs=prior_outputs,
        )
        return await self._providers[role].synthesize(provider_projection)

    async def aclose(self) -> None:
        await self._client.aclose()


def build_local_multiround_synthesizer(
    environ: Mapping[str, str] | None = None,
) -> LocalMultiRoundSynthesizer | None:
    values = os.environ if environ is None else environ
    base_url = values.get("PERFPILOT_LOCAL_AI_BASE_URL", "").strip()
    model = values.get("PERFPILOT_LOCAL_AI_MODEL", "").strip()
    token = values.get("PERFPILOT_LOCAL_AI_TOKEN", "").strip()
    if not base_url or not model or not token:
        return None
    provider_name = values.get(
        "PERFPILOT_LOCAL_AI_PROVIDER_NAME", "openai-compatible-local"
    ).strip()
    provider = LocalOpenAICompatibleRoundProvider(
        base_url=SecretStr(base_url),
        model=model,
        token=SecretStr(token),
        provider_name=provider_name,
    )
    return LocalMultiRoundSynthesizer(provider=provider)


@dataclass(frozen=True, slots=True)
class LocalRoundUsage:
    number: int
    role: RoundRole
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LocalSynthesisResult:
    output: AISynthesisOutput
    rounds: tuple[LocalRoundUsage, LocalRoundUsage, LocalRoundUsage]


class LocalMultiRoundSynthesizer:
    def __init__(self, *, provider: LocalRoundProvider) -> None:
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
                "perfpilot-local-multiround-v1",
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
        on_round: RoundObserver | None = None,
    ) -> LocalSynthesisResult:
        if not isinstance(projection, AIProjection):
            raise TypeError("projection must be an AIProjection")
        outputs: list[AISynthesisOutput] = []
        usages: list[LocalRoundUsage] = []
        roles: tuple[RoundRole, RoundRole, RoundRole] = (
            "extract",
            "review",
            "finalize",
        )
        for number, role in enumerate(roles, start=1):
            if on_round is not None:
                await on_round(number, role, "running", None)
            attempts = 0
            candidate: SynthesisCandidate | None = None
            output: AISynthesisOutput | None = None
            failure_code = "ai_output_invalid"
            retryable = True
            detail_code = "unspecified"
            while attempts < 2:
                attempts += 1
                try:
                    candidate = await self._provider.complete(
                        role=role,
                        projection=projection,
                        prior_outputs=tuple(outputs),
                    )
                    output = validate_synthesis_output(
                        projection=projection,
                        candidate=candidate.candidate_json,
                    )
                    break
                except SynthesisValidationError:
                    failure_code = "ai_output_invalid"
                    retryable = True
                    detail_code = "semantic_validation"
                except AIProviderError as error:
                    failure_code = error.stable_code
                    retryable = error.retryable
                    detail_code = error.detail_code
                    if not retryable:
                        break
                except asyncio.CancelledError:
                    raise
            if candidate is None or output is None:
                if on_round is not None:
                    await on_round(number, role, "failed", None)
                raise LocalSynthesisError(
                    failure_code,
                    round_number=number,
                    retryable=retryable,
                    detail_code=detail_code,
                )
            outputs.append(output)
            usages.append(
                LocalRoundUsage(
                    number=number,
                    role=role,
                    attempts=attempts,
                    prompt_tokens=candidate.prompt_tokens,
                    completion_tokens=candidate.completion_tokens,
                    latency_ms=candidate.latency_ms,
                )
            )
            if on_round is not None:
                await on_round(number, role, "completed", output)
        return LocalSynthesisResult(
            output=outputs[2],
            rounds=(usages[0], usages[1], usages[2]),
        )


__all__ = [
    "LocalOpenAICompatibleRoundProvider",
    "LocalMultiRoundSynthesizer",
    "LocalRoundProvider",
    "LocalRoundUsage",
    "LocalSynthesisError",
    "LocalSynthesisResult",
    "RoundObserver",
    "RoundRole",
    "RoundState",
    "build_local_round_projection",
    "build_local_multiround_synthesizer",
]
