from __future__ import annotations

import json
from pathlib import Path

import pytest

from perfpilot_api.engines.smartperfetto_transport import SmartPerfettoJsonResponse
from perfpilot_api.local_app import LocalEngineRun, SmartPerfettoLocalGateway


_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "smartperfetto_workspace_agent_v1"
    / "report-completed.json"
)


class _ReportTransport:
    def __init__(self, response: SmartPerfettoJsonResponse) -> None:
        self._response = response

    async def request_json(self, *_args: object, **_kwargs: object) -> SmartPerfettoJsonResponse:
        return self._response

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_text",
    [
        "%2Fhome%2Frivotek%2Fprivate%2Fresult.json",
        "滚动帧率样本包含上游原样百分号编码 %CC",
        "采样字段 password=private-value",
    ],
)
async def test_gateway_redacts_percent_encoded_or_invalid_private_text(
    private_text: str,
) -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    report = payload["report"]
    assert isinstance(report, dict)
    report["conversationTimeline"] = [{"role": "tool", "text": private_text}]
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    gateway = SmartPerfettoLocalGateway()
    await gateway._transport.aclose()
    gateway._transport = _ReportTransport(  # type: ignore[assignment]
        SmartPerfettoJsonResponse(200, payload, raw_body)
    )

    result = await gateway.fetch_result(LocalEngineRun("session-synthetic-001", "run-1"))

    report_payload = result.payload["report"]
    assert report_payload["conversationTimeline"][0]["text"] == "[redacted]"  # type: ignore[index]


@pytest.mark.asyncio
async def test_gateway_original_report_redacts_private_runtime_paths() -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    report = payload["report"]
    assert isinstance(report, dict)
    report["conversationTimeline"] = [
        {
            "role": "tool",
            "text": (
                "Saved result to /home/rivotek/.claude/projects/private/"
                "tool-results/call-secret.json"
            ),
        }
    ]
    raw_body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8") + b"\n"
    gateway = SmartPerfettoLocalGateway()
    await gateway._transport.aclose()
    gateway._transport = _ReportTransport(  # type: ignore[assignment]
        SmartPerfettoJsonResponse(200, payload, raw_body)
    )

    result = await gateway.fetch_result(LocalEngineRun("session-synthetic-001", "run-1"))

    assert result.original_report_bytes is not None
    original = json.loads(result.original_report_bytes)
    serialized = json.dumps(original, ensure_ascii=False)
    assert "logFile" not in original
    assert "/home/rivotek" not in serialized
    assert "/synthetic/private" not in serialized
    assert "[redacted]" in serialized
