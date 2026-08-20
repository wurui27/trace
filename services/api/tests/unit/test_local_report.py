from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

import perfpilot_api.ai.local_report as local_report
from perfpilot_api.ai.local_report import (
    LocalOpenAICompatibleReportProvider,
    LocalReportSynthesizer,
    LocalReportUsage,
    LocalSynthesisError,
    build_local_report_projection,
    build_local_report_synthesizer,
)
from perfpilot_api.ai.openai_compatible import AIProviderError, SynthesisCandidate
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import AIProjection, build_ai_projection


ROOT = Path(__file__).resolve().parents[4]


class _AlwaysEqualChecksum:
    def __eq__(self, _other: object) -> bool:
        return True


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts" / "v1" / "examples" / name).read_text(
            encoding="utf-8"
        )
    )


def _v2_candidate() -> dict[str, object]:
    document = _load("synthesis-output-v2.valid.json")
    document["source_fixes"] = []
    for conclusion in document["conclusions"]:
        conclusion["source_ref_ids"] = []
        conclusion["source_root_cause"] = "当前没有足够源码证据定位具体实现。"
    document["verdict"] = "启动关键路径被同步初始化阻塞。"
    document["executive_summary"] = "将同步查询移到首帧之后，再重复相同的冷启动场景。"
    for item in document["top_findings"]:
        item["user_impact"] = "首屏显示时间晚于现有目标。"
    for index, item in enumerate(document["recommendations"]):
        item["title"] = "延后同步查询" if index == 0 else "重复启动采集"
        item["action"] = "将查询移到首帧之后。" if index == 0 else "修改后采集相同的冷启动流程。"
        item["expected_effect"] = "移除启动关键路径中的同步等待。" if index == 0 else "依据现有阈值确认启动指标。"
    for item in document["retest_plan"]:
        item["steps"] = "使用相同流程采集五次冷启动。"
    return document


def _projection() -> AIProjection:
    core_bytes = canonical_json_bytes(_load("normalized-trace-report.valid.json"))
    core = NormalizedTraceReport(
        canonical_bytes=core_bytes,
        sha256_b64=base64.b64encode(hashlib.sha256(core_bytes).digest()).decode(),
    )
    return build_ai_projection(core, analysis_profile="auto", question=None)


def _source_projection() -> AIProjection:
    document = _load("analysis-projection-v2.valid.json")
    document["analysis_profile"] = "auto"
    payload = canonical_json_bytes(document)
    return AIProjection(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
    )


def _projection_v21() -> AIProjection:
    document = _load("analysis-projection-v2.1.valid.json")
    document["analysis_profile"] = "auto"
    payload = canonical_json_bytes(document)
    return AIProjection(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
    )


def _strong_v2_candidate() -> dict[str, object]:
    document = _v2_candidate()
    source_fix = _load("synthesis-output-v2.valid.json")["source_fixes"][0]
    document["source_fixes"] = [
        {
            **source_fix,
            "diagnosis": "启动方法在主线程同步读取设置。",
            "retest_target": "重复相同的冷启动场景并比较已有指标。",
        }
    ]
    conclusion = document["conclusions"][0]
    conclusion["source_ref_ids"] = ["97000000-0000-4000-8000-000000000001"]
    conclusion["source_root_cause"] = "启动方法同步读取设置，阻塞了首帧之前的主线程。"
    return document


def _unchecked_projection(case: str) -> AIProjection:
    projection = _projection()
    if case == "checksum":
        return AIProjection(
            canonical_bytes=projection.canonical_bytes,
            sha256_b64="not-the-projection-checksum",
        )
    if case == "checksum_type":
        return AIProjection(
            canonical_bytes=projection.canonical_bytes,
            sha256_b64=_AlwaysEqualChecksum(),  # type: ignore[arg-type]
        )
    if case == "noncanonical":
        payload = projection.canonical_bytes + b"\n"
        return AIProjection(
            canonical_bytes=payload,
            sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
        )
    if case == "oversized":
        payload = b" " * (256 * 1024 + 1)
        return AIProjection(
            canonical_bytes=payload,
            sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
        )
    document = projection.document
    if case == "private":
        document["question"] = "https://objects.invalid/private/customer.trace"
    elif case == "contract":
        document["unexpected"] = "not allowed by the projection contract"
    else:
        raise AssertionError("unknown unchecked projection case")
    payload = canonical_json_bytes(document)
    return AIProjection(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
    )


class FakeReportProvider:
    provider_name = "test-provider"
    model = "test-model"
    prompt_version = "perfpilot-report-v3"
    prompt_sha256_b64 = base64.b64encode(hashlib.sha256(b"prompt").digest()).decode()

    def __init__(self, candidates: list[bytes]) -> None:
        self.candidates = candidates
        self.calls = 0
        self.closed = False

    async def complete(self, *, projection: AIProjection) -> SynthesisCandidate:
        assert projection.document["analysis_profile"] == "auto"
        self.calls += 1
        return SynthesisCandidate(
            candidate_json=self.candidates.pop(0),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=30,
        )

    async def aclose(self) -> None:
        self.closed = True


class FailingReportProvider(FakeReportProvider):
    def __init__(self, errors: list[BaseException]) -> None:
        super().__init__([])
        self.errors = errors

    async def complete(self, *, projection: AIProjection) -> SynthesisCandidate:
        assert projection.document["analysis_profile"] == "auto"
        self.calls += 1
        raise self.errors.pop(0)


class ScriptedReportProvider(FakeReportProvider):
    def __init__(
        self,
        events: list[SynthesisCandidate | AIProviderError],
    ) -> None:
        super().__init__([])
        self.events = events
        self.retry_codes: list[str | None] = []

    async def complete(self, *, projection: AIProjection) -> SynthesisCandidate:
        return await self._complete(projection=projection, retry_code=None)

    async def complete_retry(
        self,
        *,
        projection: AIProjection,
        retry_code: str,
    ) -> SynthesisCandidate:
        return await self._complete(projection=projection, retry_code=retry_code)

    async def _complete(
        self,
        *,
        projection: AIProjection,
        retry_code: str | None,
    ) -> SynthesisCandidate:
        assert projection.document["analysis_profile"] == "auto"
        self.calls += 1
        self.retry_codes.append(retry_code)
        event = self.events.pop(0)
        if isinstance(event, AIProviderError):
            raise event
        return event


class RetryAwareReportProvider(FakeReportProvider):
    def __init__(self, *, invalid: bytes, valid: bytes) -> None:
        super().__init__([])
        self.invalid = invalid
        self.valid = valid
        self.retry_codes: list[str | None] = []

    async def complete(self, *, projection: AIProjection) -> SynthesisCandidate:
        assert projection.document["analysis_profile"] == "auto"
        self.calls += 1
        self.retry_codes.append(None)
        return SynthesisCandidate(
            candidate_json=self.invalid,
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=30,
        )

    async def complete_retry(
        self,
        *,
        projection: AIProjection,
        retry_code: str,
    ) -> SynthesisCandidate:
        assert projection.document["analysis_profile"] == "auto"
        assert retry_code == "ai_output_invalid"
        self.calls += 1
        self.retry_codes.append(retry_code)
        return SynthesisCandidate(
            candidate_json=self.valid,
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=30,
        )


@pytest.mark.asyncio
async def test_report_synthesizer_uses_one_provider_request() -> None:
    candidate = canonical_json_bytes(_v2_candidate())
    provider = FakeReportProvider([candidate])
    observed: list[tuple[int, str, str, int]] = []

    async def observe(number, role, state, attempts, _output) -> None:
        observed.append((number, role, state, attempts))

    result = await LocalReportSynthesizer(provider=provider).synthesize(
        _projection(),
        on_report=observe,
    )

    assert provider.calls == 1
    assert observed == [
        (1, "report", "running", 0),
        (1, "report", "completed", 1),
    ]
    assert result.output.document == _v2_candidate()
    assert result.rounds == (
        LocalReportUsage(1, "report", 1, 10, 20, 30),
    )


@pytest.mark.asyncio
async def test_report_synthesizer_retries_english_narrative_once_then_rejects() -> None:
    english = _load("synthesis-output-v2.valid.json")
    english["source_fixes"] = []
    for conclusion in english["conclusions"]:
        conclusion["source_ref_ids"] = []
        conclusion["problem"] = "Startup is too slow."
        conclusion["cause"] = "The trace shows blocking work on the main thread."
        conclusion["source_root_cause"] = "Source evidence is not available."
        conclusion["recommendation"] = "Move blocking work after the first frame."
    candidate = canonical_json_bytes(english)
    provider = FakeReportProvider([candidate, candidate])

    with pytest.raises(LocalSynthesisError, match="^ai_narrative_language_invalid$"):
        await LocalReportSynthesizer(provider=provider).synthesize(_projection())

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_report_synthesizer_retries_invalid_output_once() -> None:
    valid_document = _v2_candidate()
    invalid_document = dict(valid_document)
    invalid_document["executive_summary"] = (
        "Startup remains blocked by 101 unsupported delay units."
    )
    provider = RetryAwareReportProvider(
        invalid=canonical_json_bytes(invalid_document),
        valid=canonical_json_bytes(valid_document),
    )

    result = await LocalReportSynthesizer(provider=provider).synthesize(_projection())

    assert provider.calls == 2
    assert provider.retry_codes == [None, "ai_output_invalid"]
    assert len(result.rounds) == 1
    assert result.rounds[0].number == 1
    assert result.rounds[0].role == "report"
    assert result.rounds[0].attempts == 2


@pytest.mark.asyncio
async def test_report_synthesizer_retries_an_invalid_legacy_provider_without_kwargs(
) -> None:
    valid = canonical_json_bytes(_v2_candidate())
    provider = FakeReportProvider([b"{}", valid])

    result = await LocalReportSynthesizer(provider=provider).synthesize(_projection())

    assert provider.calls == 2
    assert result.rounds == (
        LocalReportUsage(1, "report", 2, 20, 40, 60),
    )


@pytest.mark.asyncio
async def test_report_synthesizer_accumulates_usage_from_invalid_candidate() -> None:
    provider = ScriptedReportProvider(
        [
            SynthesisCandidate(b"{}", 3, 5, 7),
            SynthesisCandidate(
                canonical_json_bytes(_v2_candidate()),
                11,
                13,
                17,
            ),
        ]
    )

    result = await LocalReportSynthesizer(provider=provider).synthesize(_projection())

    assert result.rounds == (
        LocalReportUsage(1, "report", 2, 14, 18, 24),
    )


@pytest.mark.asyncio
async def test_report_synthesizer_retries_retryable_provider_error() -> None:
    provider = ScriptedReportProvider(
        [
            AIProviderError(
                "ai_timeout",
                retryable=True,
                detail_code="transport_timeout",
            ),
            SynthesisCandidate(
                canonical_json_bytes(_v2_candidate()),
                10,
                20,
                30,
            ),
        ]
    )
    observed: list[tuple[str, int]] = []

    async def observe(_number, _role, state, attempts, _output) -> None:
        observed.append((state, attempts))

    result = await LocalReportSynthesizer(provider=provider).synthesize(
        _projection(),
        on_report=observe,
    )

    assert provider.calls == 2
    assert provider.retry_codes == [None, None]
    assert result.rounds == (
        LocalReportUsage(1, "report", 2, 10, 20, 30),
    )
    assert observed[-1] == ("completed", 2)


@pytest.mark.asyncio
async def test_report_synthesizer_retains_final_retryable_provider_error() -> None:
    provider = ScriptedReportProvider(
        [
            AIProviderError(
                "ai_timeout",
                retryable=True,
                detail_code="transport_timeout",
            ),
            AIProviderError(
                "ai_provider_unavailable",
                retryable=True,
                detail_code="transport_request_error",
            ),
        ]
    )
    observed: list[tuple[str, int]] = []

    async def observe(_number, _role, state, attempts, _output) -> None:
        observed.append((state, attempts))

    with pytest.raises(LocalSynthesisError) as captured:
        await LocalReportSynthesizer(provider=provider).synthesize(
            _projection(),
            on_report=observe,
        )

    assert provider.calls == 2
    assert captured.value.stable_code == "ai_provider_unavailable"
    assert captured.value.retryable is True
    assert captured.value.detail_code == "transport_request_error"
    assert observed[-1] == ("failed", 2)


@pytest.mark.asyncio
async def test_report_synthesizer_stops_after_second_invalid_output() -> None:
    provider = FakeReportProvider([b"{}", b"{}"])
    observed: list[tuple[str, int]] = []

    async def observe(_number, _role, state, attempts, _output) -> None:
        observed.append((state, attempts))

    with pytest.raises(LocalSynthesisError, match="ai_output_invalid") as captured:
        await LocalReportSynthesizer(provider=provider).synthesize(
            _projection(),
            on_report=observe,
        )

    assert provider.calls == 2
    assert captured.value.stable_code == "ai_output_invalid"
    assert captured.value.round_number == 1
    assert captured.value.retryable is True
    assert captured.value.detail_code == "semantic_validation"
    assert observed[-1] == ("failed", 2)


@pytest.mark.asyncio
async def test_v21_report_synthesizer_falls_back_after_second_invalid_output() -> None:
    provider = FakeReportProvider([b"{}", b"{}"])
    observed: list[tuple[str, int]] = []

    async def observe(_number, _role, state, attempts, _output) -> None:
        observed.append((state, attempts))

    result = await LocalReportSynthesizer(provider=provider).synthesize(
        _projection_v21(),
        on_report=observe,
    )

    assert provider.calls == 2
    assert result.ai_mode == "deterministic_fallback"
    assert result.output.document["schema_version"] == "2.1"
    assert result.output.document["conclusions"]
    assert observed[-1] == ("completed", 2)


@pytest.mark.asyncio
async def test_report_synthesizer_discards_only_unsafe_diffs_after_retry() -> None:
    candidate = _strong_v2_candidate()
    unsafe = dict(candidate["source_fixes"][0])  # type: ignore[index]
    unsafe["fix_id"] = "96000000-0000-4000-8000-000000000002"
    unsafe["diff"] += (
        "--- a/app/src/main/java/demo/Other.kt\n"
        "+++ b/app/src/main/java/demo/Other.kt\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    candidate["source_fixes"].append(unsafe)  # type: ignore[union-attr]
    payload = canonical_json_bytes(candidate)
    provider = FakeReportProvider([payload, payload])

    result = await LocalReportSynthesizer(provider=provider).synthesize(
        _source_projection()
    )

    assert provider.calls == 2
    assert [
        fix["fix_id"] for fix in result.output.document["source_fixes"]
    ] == ["96000000-0000-4000-8000-000000000001"]
    assert result.output.document["conclusions"] == candidate["conclusions"]


@pytest.mark.asyncio
async def test_report_synthesizer_restores_missing_strong_conclusion_reference() -> None:
    candidate = _strong_v2_candidate()
    candidate["source_fixes"] = []
    candidate["conclusions"][0]["source_ref_ids"] = []  # type: ignore[index]
    payload = canonical_json_bytes(candidate)
    provider = FakeReportProvider([payload, payload])

    result = await LocalReportSynthesizer(provider=provider).synthesize(
        _source_projection()
    )

    assert provider.calls == 2
    assert result.output.document["conclusions"][0]["source_ref_ids"] == [  # type: ignore[index]
        "97000000-0000-4000-8000-000000000001"
    ]
    assert result.output.document["source_fixes"] == []


@pytest.mark.asyncio
async def test_report_synthesizer_stops_after_nonretryable_provider_error() -> None:
    provider = FailingReportProvider(
        [
            AIProviderError(
                "ai_authentication_failed",
                retryable=False,
                detail_code="http_authentication",
            )
        ]
    )

    with pytest.raises(LocalSynthesisError) as captured:
        await LocalReportSynthesizer(provider=provider).synthesize(_projection())

    assert provider.calls == 1
    assert captured.value.stable_code == "ai_authentication_failed"
    assert captured.value.retryable is False
    assert captured.value.detail_code == "http_authentication"


@pytest.mark.asyncio
async def test_report_synthesizer_propagates_cancellation() -> None:
    provider = FailingReportProvider([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await LocalReportSynthesizer(provider=provider).synthesize(_projection())

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_report_synthesizer_closes_the_provider() -> None:
    provider = FakeReportProvider([])
    synthesizer = LocalReportSynthesizer(provider=provider)

    await synthesizer.aclose()

    assert provider.closed is True


def test_report_synthesizer_requires_an_ai_projection() -> None:
    provider = FakeReportProvider([])

    with pytest.raises(TypeError, match="projection must be an AIProjection"):
        asyncio.run(
            LocalReportSynthesizer(provider=provider).synthesize(  # type: ignore[arg-type]
                object()
            )
        )

    assert provider.calls == 0


def test_local_provider_factory_requires_complete_configuration() -> None:
    assert build_local_report_synthesizer({}) is None
    assert (
        build_local_report_synthesizer(
            {
                "PERFPILOT_LOCAL_AI_BASE_URL": "https://api.example.com/v1/",
                "PERFPILOT_LOCAL_AI_MODEL": "model-a",
            }
        )
        is None
    )


def test_local_provider_factory_exposes_non_secret_report_metadata() -> None:
    synthesizer = build_local_report_synthesizer(
        {
            "PERFPILOT_LOCAL_AI_BASE_URL": "https://api.example.com/v1/",
            "PERFPILOT_LOCAL_AI_MODEL": "model-a",
            "PERFPILOT_LOCAL_AI_TOKEN": "not-a-real-token",
            "PERFPILOT_LOCAL_AI_PROVIDER_NAME": "local-deepseek",
        }
    )

    assert synthesizer is not None
    try:
        assert synthesizer.provider_name == "local-deepseek"
        assert synthesizer.model == "model-a"
        assert synthesizer.prompt_version == "perfpilot-finding-report-v4"
        assert "not-a-real-token" not in repr(synthesizer)
        assert "not-a-real-token" not in repr(synthesizer._provider)
    finally:
        asyncio.run(synthesizer.aclose())


def test_local_provider_factory_forwards_explicit_thinking_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class CapturingProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        local_report,
        "LocalOpenAICompatibleReportProvider",
        CapturingProvider,
    )

    synthesizer = build_local_report_synthesizer(
        {
            "PERFPILOT_LOCAL_AI_BASE_URL": "https://api.example.com/v1/",
            "PERFPILOT_LOCAL_AI_MODEL": "model-a",
            "PERFPILOT_LOCAL_AI_TOKEN": "not-a-real-token",
            "PERFPILOT_LOCAL_AI_PROVIDER_NAME": "provider-a",
            "PERFPILOT_LOCAL_AI_THINKING": "disabled",
        }
    )

    assert synthesizer is not None
    assert captured["thinking_mode"] == "disabled"


def test_local_provider_factory_rejects_invalid_thinking_mode() -> None:
    with pytest.raises(ValueError, match="^local AI thinking mode is invalid$"):
        build_local_report_synthesizer(
            {
                "PERFPILOT_LOCAL_AI_BASE_URL": "https://api.example.com/v1/",
                "PERFPILOT_LOCAL_AI_MODEL": "model-a",
                "PERFPILOT_LOCAL_AI_TOKEN": "not-a-real-token",
                "PERFPILOT_LOCAL_AI_THINKING": "sometimes",
            }
        )


def test_json_object_report_envelope_supplies_the_output_schema() -> None:
    report_projection = build_local_report_projection(_projection())

    document = report_projection.document
    assert set(document) == {
        "allowed_numeric_spellings",
        "authoritative_projection",
        "output_schema",
        "round_role",
    }
    assert document["round_role"] == "report"
    assert document["output_schema"]["$id"] == (
        "https://perfpilot.internal/contracts/v1/ai/synthesis-output.schema.json"
    )
    assert document["allowed_numeric_spellings"] == ["700", "812.4"]


@pytest.mark.parametrize(
    "case",
    [
        "private",
        "contract",
        "checksum",
        "checksum_type",
        "noncanonical",
        "oversized",
    ],
)
def test_report_projection_rejects_untrusted_projection(case: str) -> None:
    with pytest.raises(ValueError, match="^local AI report input is invalid$"):
        build_local_report_projection(_unchecked_projection(case))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "private",
        "contract",
        "checksum",
        "checksum_type",
        "noncanonical",
        "oversized",
    ],
)
async def test_report_synthesizer_rejects_untrusted_projection_before_provider_call(
    case: str,
) -> None:
    provider = FakeReportProvider([])

    with pytest.raises(ValueError, match="^local AI report input is invalid$"):
        await LocalReportSynthesizer(provider=provider).synthesize(
            _unchecked_projection(case)
        )

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_openai_compatible_report_provider_uses_bounded_json_configuration(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    synthesize_kwargs: list[dict[str, object]] = []
    instances: list[CapturingSynthesisProvider] = []

    class CapturingSynthesisProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.closed = False
            instances.append(self)

        async def synthesize(self, projection: AIProjection, **kwargs) -> SynthesisCandidate:
            captured["projection"] = projection
            synthesize_kwargs.append(kwargs)
            return SynthesisCandidate(b"{}", 1, 2, 3)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        local_report,
        "OpenAICompatibleSynthesisProvider",
        CapturingSynthesisProvider,
    )
    provider = LocalOpenAICompatibleReportProvider(
        base_url=SecretStr("https://api.example.com/v1/"),
        model="model-a",
        token=SecretStr("not-a-real-token"),
        provider_name="provider-a",
        thinking_mode="disabled",
    )

    candidate = await provider.complete(projection=_projection())
    retry_candidate = await provider.complete_retry(
        projection=_projection(),
        retry_code="ai_output_invalid",
    )
    await provider.aclose()

    timeout = captured["timeout"]
    assert "client" not in captured
    assert captured["response_format"] == "json_object"
    assert captured["max_completion_tokens"] == 8192
    assert captured["max_response_bytes"] == 128 * 1024
    assert captured["thinking_mode"] == "disabled"
    assert timeout.connect == 10.0
    assert timeout.read == 120.0
    assert timeout.write == 30.0
    assert timeout.pool == 10.0
    assert captured["projection"].document["round_role"] == "report"
    assert synthesize_kwargs == [
        {},
        {"retry_code": "ai_output_invalid"},
    ]
    assert candidate.prompt_tokens == 1
    assert retry_candidate.prompt_tokens == 1
    assert instances[0].closed is True


def test_local_report_prompt_forbids_ascii_digits_in_every_narrative_field() -> None:
    instruction = local_report._local_prompt().system_instruction

    assert "must not contain ASCII digits" in instruction
    for field_name in (
        "executive_summary",
        "user_impact",
        "recommendation title",
        "recommendation action",
        "recommendation expected_effect",
        "retest steps",
        "limitation summary",
    ):
        assert field_name in instruction


@pytest.mark.asyncio
async def test_openai_compatible_report_provider_owns_and_closes_client() -> None:
    provider = LocalOpenAICompatibleReportProvider(
        base_url=SecretStr("https://api.example.com/v1/"),
        model="model-a",
        token=SecretStr("not-a-real-token"),
        provider_name="provider-a",
    )
    inner_provider = provider._provider
    client = inner_provider.client

    try:
        assert inner_provider._owns_client is True
        assert client.is_closed is False
    finally:
        await provider.aclose()

    assert client.is_closed is True


def test_invalid_provider_url_does_not_allocate_client(monkeypatch) -> None:
    allocations = 0

    class TrackingClient:
        follow_redirects = False

        def __init__(self, **_kwargs) -> None:
            nonlocal allocations
            allocations += 1

    monkeypatch.setattr(local_report.httpx, "AsyncClient", TrackingClient)

    with pytest.raises(ValueError, match="^AI provider URL is invalid$"):
        LocalOpenAICompatibleReportProvider(
            base_url=SecretStr("https://api.example.com/not-v1/"),
            model="model-a",
            token=SecretStr("not-a-real-token"),
            provider_name="provider-a",
        )

    assert allocations == 0
