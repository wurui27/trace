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
    def __init__(
        self,
        response: SmartPerfettoJsonResponse,
        *,
        html: bytes = b"<!DOCTYPE html><html><body>native</body></html>",
    ) -> None:
        self._response = response
        self.html = html
        self.html_requests: list[tuple[str, str]] = []

    async def request_json(self, *_args: object, **_kwargs: object) -> SmartPerfettoJsonResponse:
        return self._response

    async def request_html(self, path: str, *, workspace_id: str) -> bytes:
        self.html_requests.append((path, workspace_id))
        return self.html

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
async def test_gateway_preserves_native_html_and_redacts_machine_evidence() -> None:
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
    original_html = (
        b'<!DOCTYPE html>\n<html><body data-order="b a">'
        b'\xe4\xb8\xad\\u6587</body></html>\n'
    )
    transport = _ReportTransport(
        SmartPerfettoJsonResponse(200, payload, raw_body),
        html=original_html,
    )
    gateway._transport = transport  # type: ignore[assignment]

    result = await gateway.fetch_result(LocalEngineRun("session-synthetic-001", "run-1"))

    serialized = json.dumps(result.payload["report"], ensure_ascii=False)
    assert "/home/rivotek" not in serialized
    assert "/synthetic/private" not in serialized
    assert "[redacted]" in serialized
    assert result.original_report_html_bytes == original_html
    assert transport.html_requests == [
        ("/api/reports/report-synthetic-001/export", "default-workspace")
    ]
