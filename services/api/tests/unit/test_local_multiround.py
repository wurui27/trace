from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from perfpilot_api.ai.local_multiround import (
    LocalMultiRoundSynthesizer,
    LocalSynthesisError,
    build_local_round_projection,
    build_local_multiround_synthesizer,
)
from perfpilot_api.ai.openai_compatible import SynthesisCandidate
from perfpilot_api.ai.synthesis import validate_synthesis_output
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import AIProjection, build_ai_projection


ROOT = Path(__file__).resolve().parents[4]


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts" / "v1" / "examples" / name).read_text(encoding="utf-8")
    )


def _projection() -> AIProjection:
    core_bytes = canonical_json_bytes(_load("normalized-trace-report.valid.json"))
    core = NormalizedTraceReport(
        canonical_bytes=core_bytes,
        sha256_b64=base64.b64encode(hashlib.sha256(core_bytes).digest()).decode(),
    )
    return build_ai_projection(core, analysis_profile="auto", question=None)


class FakeRoundProvider:
    def __init__(self, candidates: list[bytes]) -> None:
        self.candidates = candidates
        self.roles: list[str] = []
        self.prior_counts: list[int] = []

    async def complete(self, *, role, projection, prior_outputs):
        self.roles.append(role)
        self.prior_counts.append(len(prior_outputs))
        return SynthesisCandidate(
            candidate_json=self.candidates.pop(0),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=30,
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runner_executes_three_validated_rounds_in_order() -> None:
    candidate = canonical_json_bytes(_load("synthesis-output.valid.json"))
    provider = FakeRoundProvider([candidate, candidate, candidate])
    observed: list[tuple[int, str, str]] = []

    async def observe(number, role, state, _output) -> None:
        observed.append((number, role, state))

    result = await LocalMultiRoundSynthesizer(provider=provider).synthesize(
        _projection(),
        on_round=observe,
    )

    assert provider.roles == ["extract", "review", "finalize"]
    assert provider.prior_counts == [0, 1, 2]
    assert observed == [
        (1, "extract", "running"),
        (1, "extract", "completed"),
        (2, "review", "running"),
        (2, "review", "completed"),
        (3, "finalize", "running"),
        (3, "finalize", "completed"),
    ]
    assert result.output.document == _load("synthesis-output.valid.json")
    assert [(item.number, item.role) for item in result.rounds] == [
        (1, "extract"),
        (2, "review"),
        (3, "finalize"),
    ]


@pytest.mark.asyncio
async def test_runner_stops_before_later_rounds_after_invalid_references() -> None:
    provider = FakeRoundProvider([b"{}", b"{}"])

    with pytest.raises(LocalSynthesisError, match="ai_output_invalid"):
        await LocalMultiRoundSynthesizer(provider=provider).synthesize(_projection())

    assert provider.roles == ["extract", "extract"]


@pytest.mark.asyncio
async def test_runner_retries_one_invalid_candidate_then_continues() -> None:
    candidate = canonical_json_bytes(_load("synthesis-output.valid.json"))
    provider = FakeRoundProvider([b"{}", candidate, candidate, candidate])

    result = await LocalMultiRoundSynthesizer(provider=provider).synthesize(_projection())

    assert provider.roles == ["extract", "extract", "review", "finalize"]
    assert result.rounds[0].attempts == 2
    assert result.rounds[1].attempts == 1
    assert result.rounds[2].attempts == 1


@pytest.mark.asyncio
async def test_runner_closes_the_provider() -> None:
    candidate = canonical_json_bytes(_load("synthesis-output.valid.json"))
    provider = FakeRoundProvider([candidate, candidate, candidate])
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    provider.aclose = close  # type: ignore[method-assign]
    runner = LocalMultiRoundSynthesizer(provider=provider)

    await runner.aclose()

    assert closed is True


def test_local_provider_factory_requires_complete_configuration() -> None:
    assert build_local_multiround_synthesizer({}) is None
    assert (
        build_local_multiround_synthesizer(
            {
                "PERFPILOT_LOCAL_AI_BASE_URL": "https://api.example.com/v1/",
                "PERFPILOT_LOCAL_AI_MODEL": "model-a",
            }
        )
        is None
    )


def test_local_provider_factory_exposes_non_secret_report_metadata() -> None:
    runner = build_local_multiround_synthesizer(
        {
            "PERFPILOT_LOCAL_AI_BASE_URL": "https://api.example.com/v1/",
            "PERFPILOT_LOCAL_AI_MODEL": "model-a",
            "PERFPILOT_LOCAL_AI_TOKEN": "not-a-real-token",
            "PERFPILOT_LOCAL_AI_PROVIDER_NAME": "local-deepseek",
        }
    )

    assert runner is not None
    assert runner.provider_name == "local-deepseek"
    assert runner.model == "model-a"
    assert runner.prompt_version == "perfpilot-local-multiround-v1"
    assert "not-a-real-token" not in repr(runner)


def test_json_object_round_envelope_supplies_the_output_schema() -> None:
    projection = _projection()
    candidate = canonical_json_bytes(_load("synthesis-output.valid.json"))
    prior = validate_synthesis_output(projection=projection, candidate=candidate)

    round_projection = build_local_round_projection(
        role="review",
        projection=projection,
        prior_outputs=(prior,),
    )

    document = round_projection.document
    assert set(document) == {
        "allowed_numeric_spellings",
        "authoritative_projection",
        "output_schema",
        "prior_validated_outputs",
        "round_role",
    }
    assert document["output_schema"]["$id"] == (
        "https://perfpilot.internal/contracts/v1/ai/synthesis-output.schema.json"
    )
    assert document["prior_validated_outputs"] == [prior.document]
    assert document["allowed_numeric_spellings"] == ["700", "812.4"]
