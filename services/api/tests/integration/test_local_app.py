from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from perfpilot_api.ai.local_multiround import LocalMultiRoundSynthesizer
from perfpilot_api.ai.openai_compatible import SynthesisCandidate
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.local_app import LocalEngineRun, _public_origin, create_local_app
from perfpilot_api.local_analysis_store import LocalAnalysisStore
from perfpilot_api.local_device_capture import LocalApkMetadata, LocalDeviceCapture
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract


class _FakeSmartPerfettoGateway:
    def __init__(self, result: EngineResult) -> None:
        self.result = result
        self.submissions: list[tuple[bytes, str, str | None]] = []
        self.cancel_calls: list[LocalEngineRun] = []

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        self.submissions.append((trace_path.read_bytes(), profile, question))
        return LocalEngineRun(session_id="session-local-1", run_id="run-local-1")

    async def status(self, run: LocalEngineRun) -> str:
        assert run.session_id == "session-local-1"
        return "completed"

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        assert run.run_id == "run-local-1"
        return self.result

    async def cancel(self, run: LocalEngineRun) -> None:
        self.cancel_calls.append(run)

    async def aclose(self) -> None:
        return None


def test_local_runtime_accepts_only_loopback_or_private_lan_http_origins() -> None:
    assert _public_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert _public_origin("http://10.166.0.125:8000") == "http://10.166.0.125:8000"

    with pytest.raises(ValueError, match="loopback or private LAN HTTP"):
        _public_origin("http://8.8.8.8:8000")


def test_local_runtime_allows_configured_private_lan_web_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PERFPILOT_LOCAL_WEB_ORIGIN", "http://10.166.0.125:3000")
    app = create_local_app(
        data_root=tmp_path,
        public_origin="http://10.166.0.125:8000",
    )

    with TestClient(app) as client:
        response = client.options(
            "/local/v1/uploads/example",
            headers={
                "Origin": "http://10.166.0.125:3000",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "content-type,x-amz-checksum-sha256"
                ),
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://10.166.0.125:3000"


class _BlockingSmartPerfettoGateway(_FakeSmartPerfettoGateway):
    async def status(self, run: LocalEngineRun) -> str:
        assert run.session_id == "session-local-1"
        return "running"


@dataclass(frozen=True)
class _FakeDevice:
    serial: str
    manufacturer: str
    model: str
    android_version: str
    api_level: int


@dataclass(frozen=True)
class _FakeDeviceStatus:
    state: str
    device: _FakeDevice | None


class _FakeDeviceProbe:
    def __init__(self, status: _FakeDeviceStatus) -> None:
        self.status = status
        self.calls = 0

    async def inspect(self) -> _FakeDeviceStatus:
        self.calls += 1
        return self.status


class _FakeLocalDeviceCaptureGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, Path]] = []

    async def capture(
        self,
        *,
        apk_path: Path,
        serial: str,
        workspace: Path,
    ) -> LocalDeviceCapture:
        self.calls.append((apk_path.read_bytes(), serial, workspace))
        startup_trace = workspace / "startup.perfetto-trace"
        scroll_trace = workspace / "scroll.perfetto-trace"
        memory_evidence = workspace / "memory-evidence.tar"
        startup_trace.write_bytes(b"captured-startup-trace")
        scroll_trace.write_bytes(b"captured-scroll-trace")
        memory_evidence.write_bytes(b"captured-memory-evidence")
        return LocalDeviceCapture(
            metadata=LocalApkMetadata(
                package_name="com.example.perfpilot",
                version_name="1.2.3",
                version_code=123,
                launch_activity="com.example.perfpilot/.MainActivity",
                min_sdk=26,
                target_sdk=35,
                supported_abis=("arm64-v8a",),
                has_native_libraries=True,
            ),
            startup_trace=startup_trace,
            scroll_trace=scroll_trace,
            memory_evidence=memory_evidence,
        )


class _FakeLocalMemoryAnalysisGateway:
    engine_commit_sha = "d5514972ced78c3faa7fc17589c1ea9231645056"

    def __init__(self, result: EngineResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def analyze(self, **kwargs: object) -> EngineResult:
        self.calls.append(kwargs)
        return self.result

    async def aclose(self) -> None:
        self.close_calls += 1


class _ProjectionRoundProvider:
    provider_name = "test-provider"
    model = "test-model"
    prompt_version = "perfpilot-local-multiround-test"
    prompt_sha256_b64 = base64.b64encode(hashlib.sha256(b"test-prompt").digest()).decode(
        "ascii"
    )

    async def complete(self, *, role, projection, prior_outputs) -> SynthesisCandidate:
        del role, prior_outputs
        projected = projection.document
        findings = []
        recommendations = []
        retest_plan = []
        for scenario in projected["scenarios"]:
            for finding in scenario["findings"]:
                evidence_ids = list(finding["evidence_ids"])
                findings.append(
                    {
                        "finding_id": finding["finding_id"],
                        "evidence_ids": evidence_ids,
                        "user_impact": finding["summary"],
                    }
                )
                if finding["status"] in {"confirmed", "suspected"} and evidence_ids:
                    recommendations.append(
                        {
                            "priority": "p1",
                            "title": finding["title"],
                            "action": "修复该性能问题，并使用相同场景复测。",
                            "expected_effect": "降低该问题对用户体验的影响。",
                            "finding_ids": [finding["finding_id"]],
                            "evidence_ids": evidence_ids,
                        }
                    )
            metric_ids = [
                metric["metric_id"]
                for metric in scenario["metrics"]
                if metric["status"] == "available"
            ]
            if metric_ids:
                retest_plan.append(
                    {
                        "mode": "verify_metric",
                        "scenario_type": scenario["scenario_type"],
                        "metric_ids": metric_ids,
                        "limitation_ids": [],
                        "steps": "使用相同设备和场景重新采集 Trace。",
                        "success_condition": "improve_from_baseline",
                        "failure_condition": "threshold_missed",
                    }
                )
        document = {
            "schema_version": "1.0",
            "executive_summary": "三轮测试 AI 已完成证据复核。",
            "top_findings": findings[:5],
            "recommendations": recommendations[:10],
            "retest_plan": retest_plan[:5],
            "limitations": [
                {
                    "limitation_id": item["limitation_id"],
                    "summary": item["summary"],
                }
                for item in projected["limitations"]
            ],
        }
        return SynthesisCandidate(
            candidate_json=canonical_json_bytes(document),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=5,
        )

    async def aclose(self) -> None:
        return None


def _test_synthesizer() -> LocalMultiRoundSynthesizer:
    return LocalMultiRoundSynthesizer(provider=_ProjectionRoundProvider())


def _smartperfetto_result() -> EngineResult:
    fixture = Path(__file__).resolve().parents[1] / (
        "fixtures/canonical_results/smartperfetto-result-contract-1.0.0.json"
    )
    canonical = json.loads(fixture.read_text("utf-8"))
    payload = canonical["result"]["payload"]
    for private_key in ("actions", "workspaceId", "runId"):
        payload["report"].pop(private_key, None)
    payload["report"]["reportId"] = payload["reportId"]
    return EngineResult(
        contract="workspace-agent-v1",
        state="completed",
        payload=payload,
    )


def _live_smartperfetto_result() -> EngineResult:
    return EngineResult(
        contract="workspace-agent-v1",
        state="completed",
        payload={
            "reportId": "live-report-1",
            "report": {
                "reportId": "live-report-1",
                "summary": {
                    "conclusion": "冷启动为 301.84ms；JIT 编译和 Debug 模式是主要优化点。",
                    "confidence": 0.9,
                },
                "resultContract": {
                    "version": "1.0.0",
                    "dataEnvelopes": [
                        {
                            "meta": {
                                "type": "skill_result",
                                "source": "startup_analysis:get_startups",
                                "stepId": "get_startups",
                                "evidenceRefId": "data:skill:startup_analysis:get_startups",
                                "identityResolution": {
                                    "version": "identity_contract@1",
                                    "status": "verified",
                                    "target": {
                                        "packageName": "com.example.app",
                                        "processName": "com.example.app",
                                    },
                                    "processes": [{"upid": 9, "pid": 99}],
                                    "threads": [{"tid": 99, "role": "app_main"}],
                                },
                            },
                            "data": {
                                "columns": [
                                    "startup_id",
                                    "dur_ms",
                                    "ttid_ms",
                                    "start_ts",
                                    "end_ts",
                                ],
                                "rows": [[1, 301.84, 299.82, 1000, 302840000]],
                            },
                            "display": {
                                "title": "检测到的启动事件",
                                "columns": [
                                    {"name": "dur_ms", "label": "启动耗时", "unit": "ms"},
                                    {"name": "ttid_ms", "label": "TTID", "unit": "ms"},
                                ],
                            },
                        }
                    ],
                    "diagnostics": [
                        {
                            "id": "jit-active",
                            "severity": "critical",
                            "title": "JIT 编译活跃",
                            "description": "启动期间存在大量 JIT 编译。",
                            "confidence": 0.95,
                            "evidence": [
                                {
                                    "text": "JIT 编译 1904 次。\n\n**建议**：部署 Baseline Profile。"
                                }
                            ],
                        }
                    ],
                    "actions": [
                        {
                            "id": "fix-jit",
                            "label": "部署 Baseline Profile",
                            "priority": "high",
                            "sourceDiagnosticId": "jit-active",
                        }
                    ],
                },
                "identityResolutions": [],
            },
        },
    )


def _android_memory_result() -> EngineResult:
    return EngineResult(
        contract="android-memory-ai-context-1.2",
        state="completed",
        payload={
            "context_type": "android-memory-ai-context",
            "schema_version": "1.2",
            "generator": {"name": "android-memory-ai", "version": "1.2.0"},
            "request": {
                "intent": "quick-triage",
                "evaluated_intents": ["quick-triage"],
            },
            "evidence": {
                "coverage": {
                    "level": "strong",
                    "available": ["meminfo"],
                    "missing_required": [],
                    "missing_supporting": [],
                    "missing_any_of": [],
                    "inadequate": [],
                },
                "accounting_ledger": {
                    "schema_version": "1.0",
                    "status": "available",
                    "rows": [
                        {
                            "name": "TOTAL",
                            "meminfo": {
                                "pss_total_kb": 123456,
                                "private_dirty_kb": 45678,
                                "private_clean_kb": 987,
                                "swap_pss_kb": 321,
                                "rss_total_kb": 150000,
                            },
                        },
                        {
                            "name": "Native Heap",
                            "meminfo": {
                                "pss_total_kb": 32000,
                                "private_dirty_kb": 30000,
                                "private_clean_kb": 0,
                                "swap_pss_kb": 12,
                                "rss_total_kb": 34000,
                            },
                        },
                    ],
                },
            },
            "analysis_contract": {
                "support_level": "strong",
                "primary_intent_support_level": "strong",
                "privacy": {
                    "raw_contents_embedded": False,
                    "local_paths_included": False,
                },
            },
            "next_evidence": [],
            "limitations": [],
        },
    )


def _create_trace_analysis(
    client: TestClient,
    *,
    team_id: str,
    headers: dict[str, str],
    trace: bytes = b"background-local-trace",
) -> tuple[str, str]:
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")
    response = client.post(
        f"/v1/teams/{team_id}/analyses",
        headers=headers,
        json={
            "schema_version": "1.0",
            "analysis_mode": "trace_upload",
            "analysis_profile": "startup",
            "question": "首帧为什么慢？",
            "inputs": [
                {
                    "kind": "trace",
                    "mime": "application/octet-stream",
                    "size": len(trace),
                    "sha256_b64": checksum,
                }
            ],
        },
    )
    assert response.status_code == 201
    return response.json()["analysis_id"], checksum


def _upload_and_finalize_trace(
    client: TestClient,
    *,
    team_id: str,
    analysis_id: str,
    headers: dict[str, str],
    checksum: str,
    trace: bytes = b"background-local-trace",
) -> None:
    slot_response = client.post(
        f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
        headers=headers,
        json={
            "artifact_kind": "trace",
            "mime": "application/octet-stream",
            "size": len(trace),
            "sha256_b64": checksum,
        },
    )
    assert slot_response.status_code == 201
    slot = slot_response.json()["upload"]
    put = urlsplit(slot["put_url"])
    assert client.put(
        f"{put.path}?{put.query}",
        content=trace,
        headers=slot["required_headers"],
    ).status_code == 200
    assert client.post(
        f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
        headers=headers,
        json={
            "upload_id": slot["upload_id"],
            "sha256_b64": checksum,
            "size": len(trace),
        },
    ).status_code == 200


def test_local_app_reports_the_device_currently_connected_over_adb(tmp_path: Path) -> None:
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial="0123456789ABCDEF",
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        response = client.get("/v1/device")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "state": "connected",
        "device": {
            "serial": "0123456789ABCDEF",
            "manufacturer": "UNISOC",
            "model": "uis7870_2h10_car_c200_6",
            "name": "UNISOC uis7870_2h10_car_c200_6",
            "os": "Android 13",
            "api_level": 33,
        },
    }
    assert device_probe.calls == 1


def test_local_app_exposes_connected_adb_device_through_team_directory(
    tmp_path: Path,
) -> None:
    serial = "0123456789ABCDEF"
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial=serial,
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        first = client.get(f"/v1/teams/{team_id}/devices")
        second = client.get(f"/v1/teams/{team_id}/devices")

    assert first.status_code == 200
    assert second.status_code == 200
    device = first.json()["devices"][0]
    assert device == {
        "device_id": second.json()["devices"][0]["device_id"],
        "agent_id": "71000000-0000-4000-8000-000000000001",
        "agent_name": "本机 ADB",
        "serial_suffix": "CDEF",
        "manufacturer": "UNISOC",
        "model": "uis7870_2h10_car_c200_6",
        "android_release": "13",
        "api_level": 33,
        "connection_type": "usb",
        "adb_state": "device",
        "state": "ready",
        "last_seen_at": device["last_seen_at"],
    }
    assert serial not in json.dumps(first.json())
    assert datetime.fromisoformat(device["last_seen_at"]).tzinfo is not None
    assert device_probe.calls == 2


def test_local_team_device_directory_is_empty_without_one_ready_device(
    tmp_path: Path,
) -> None:
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(state="disconnected", device=None)
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        response = client.get(f"/v1/teams/{team_id}/devices")

    assert response.status_code == 200
    assert response.json() == {"schema_version": "1.0", "devices": []}


def test_local_app_creates_device_analysis_with_embedded_apk_upload(
    tmp_path: Path,
) -> None:
    serial = "0123456789ABCDEF"
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial=serial,
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    apk = b"local-device-apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        device_probe=device_probe,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
    )

    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        device_id = client.get(f"/v1/teams/{team_id}/devices").json()["devices"][0][
            "device_id"
        ]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "device",
                "device_id": device_id,
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        )

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["analysis_mode"] == "device"
    assert payload["device_id"] == device_id
    assert payload["application_version_id"] is None
    assert payload["application_metadata"] is None
    assert payload["active_lease"] is None
    assert payload["started_at"] is None
    assert payload["completed_at"] is None
    assert payload["report_available"] is False
    assert [item["scenario_type"] for item in payload["scenarios"]] == [
        "cold_start",
        "scroll",
        "memory_cycle",
    ]
    assert {item["state"] for item in payload["scenarios"]} == {"awaiting_input"}
    assert payload["sample_verdict_counts"] == {
        "valid": 0,
        "invalid": 0,
        "pending": 0,
        "validation_error": 0,
        "total": 0,
    }
    upload = payload["apk_upload"]
    assert upload["state"] == "pending"
    assert upload["artifact_kind"] == "apk"
    assert upload["mime"] == "application/vnd.android.package-archive"
    assert upload["size"] == len(apk)
    assert upload["sha256_b64"] == checksum
    assert datetime.fromisoformat(upload["expires_at"]).tzinfo is not None
    assert urlsplit(upload["put_url"]).path.startswith("/local/v1/uploads/")
    assert upload["required_headers"] == {
        "Content-Type": "application/vnd.android.package-archive",
        "x-amz-checksum-sha256": checksum,
    }
    assert "ai_rounds" not in payload
    assert "source_analysis" not in payload
    assert serial not in json.dumps(payload)


def test_local_device_analysis_captures_in_background_and_publishes_report(
    tmp_path: Path,
) -> None:
    serial = "0123456789ABCDEF"
    device_probe = _FakeDeviceProbe(
        _FakeDeviceStatus(
            state="connected",
            device=_FakeDevice(
                serial=serial,
                manufacturer="UNISOC",
                model="uis7870_2h10_car_c200_6",
                android_version="13",
                api_level=33,
            ),
        )
    )
    capture_gateway = _FakeLocalDeviceCaptureGateway()
    memory_gateway = _FakeLocalMemoryAnalysisGateway(_android_memory_result())
    smartperfetto = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    apk = b"installable-local-device-apk"
    checksum = base64.b64encode(hashlib.sha256(apk).digest()).decode("ascii")
    app = create_local_app(
        gateway=smartperfetto,
        synthesizer=_test_synthesizer(),
        device_probe=device_probe,
        device_capture_gateway=capture_gateway,
        memory_analysis_gateway=memory_gateway,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        device_id = client.get(f"/v1/teams/{team_id}/devices").json()["devices"][0][
            "device_id"
        ]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "device",
                "device_id": device_id,
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": len(apk),
                    "sha256_b64": checksum,
                },
            },
        ).json()
        analysis_id = created["analysis_id"]
        slot = created["apk_upload"]
        put = urlsplit(slot["put_url"])
        assert client.put(
            f"{put.path}?{put.query}", content=apk, headers=slot["required_headers"]
        ).status_code == 200
        assert client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(apk),
            },
        ).status_code == 200

        terminal = None
        for _ in range(200):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        assert terminal is not None
        assert terminal["state"] == "partially_completed"
        assert terminal["report_available"] is True
        assert terminal["application_version_id"] is not None
        assert terminal["application_metadata"] == {
            "package_name": "com.example.perfpilot",
            "version_name": "1.2.3",
            "version_code": 123,
            "launch_activity": "com.example.perfpilot/.MainActivity",
            "min_sdk": 26,
            "target_sdk": 35,
            "supported_abis": ["arm64-v8a"],
            "has_native_libraries": True,
        }
        assert terminal["started_at"] is not None
        assert terminal["completed_at"] is not None
        assert "ai_rounds" not in terminal
        assert "source_analysis" not in terminal
        assert [item["state"] for item in terminal["scenarios"]] == [
            "failed",
            "failed",
            "completed",
        ]
        assert terminal["scenarios"][0]["failure"]["code"] == "insufficient_data"
        assert terminal["scenarios"][1]["failure"]["code"] == "insufficient_data"
        assert terminal["scenarios"][2]["failure"] is None

        report_response = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert capture_gateway.calls
    captured_apk, captured_serial, workspace = capture_gateway.calls[0]
    assert captured_apk == apk
    assert captured_serial == serial
    assert workspace.parent.name == "device-captures"
    assert smartperfetto.submissions == [
        (b"captured-startup-trace", "startup", None),
        (b"captured-scroll-trace", "scroll", None),
    ]
    assert len(memory_gateway.calls) == 1
    assert memory_gateway.calls[0]["analysis_id"] == UUID(analysis_id)
    assert memory_gateway.calls[0]["package_name"] == "com.example.perfpilot"
    assert memory_gateway.calls[0]["android_release"] == "13"
    assert memory_gateway.calls[0]["api_level"] == 33
    evidence_path = memory_gateway.calls[0]["evidence_path"]
    assert isinstance(evidence_path, Path)
    assert evidence_path.name == "memory-evidence.tar"
    assert memory_gateway.close_calls == 1
    assert report_response.status_code == 200
    report = validate_contract("analysis-report", report_response.json())
    assert report["analysis_mode"] == "device"
    assert [item["scenario_type"] for item in report["scenario_reports"]] == [
        "startup",
        "scroll",
        "memory_cycle",
    ]
    memory = report["scenario_reports"][2]
    assert memory["result_state"] == "completed"
    assert memory["failure"] is None
    assert memory["bundle"]["metrics"]
    metric_values = {
        metric["name"]: metric["numeric_value"]
        for metric in memory["bundle"]["metrics"]
    }
    assert metric_values["memory.meminfo.total.pss_kb"] == 123456
    assert metric_values["memory.meminfo.native_heap.private_dirty_kb"] == 30000
    assert serial not in json.dumps(report)


def test_local_app_lists_one_active_analysis_and_rejects_a_second(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, _ = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )

        active = client.get(
            f"/v1/teams/{team_id}/analyses?status=active&limit=1"
        )
        duplicate = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "trace_upload",
                "analysis_profile": "auto",
                "question": None,
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": 1,
                        "sha256_b64": base64.b64encode(
                            hashlib.sha256(b"x").digest()
                        ).decode("ascii"),
                    }
                ],
            },
        )

    assert active.status_code == 200
    assert [item["analysis_id"] for item in active.json()["analyses"]] == [
        analysis_id
    ]
    assert active.json()["analyses"][0]["cancel_requested_at"] is None
    assert duplicate.status_code == 409


def test_local_app_cancel_stops_smartperfetto_and_persists_the_terminal_state(
    tmp_path: Path,
) -> None:
    gateway = _BlockingSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )

        running = None
        for _ in range(100):
            running = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if running["source_analysis"]["session_id"] is not None:
                break
            time.sleep(0.01)
        assert running is not None
        assert running["state"] == "analyzing"

        assert client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel"
        ).status_code == 403
        canceled = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel",
            headers=headers,
        )
        repeated = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/cancel",
            headers=headers,
        )
        after = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}"
        )
        active = client.get(
            f"/v1/teams/{team_id}/analyses?status=active&limit=1"
        )

    assert canceled.status_code == 202
    assert canceled.json()["state"] == "canceled"
    assert isinstance(canceled.json()["cancel_requested_at"], str)
    assert all(
        stage["state"] in {"completed", "canceled"}
        for stage in canceled.json()["stages"]
    )
    assert repeated.status_code == 200
    assert repeated.json()["cancel_requested_at"] == canceled.json()[
        "cancel_requested_at"
    ]
    assert after.json()["state"] == "canceled"
    assert active.json()["analyses"] == []
    assert gateway.cancel_calls == [
        LocalEngineRun(session_id="session-local-1", run_id="run-local-1")
    ]

    restored_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(restored_app) as client:
        restored = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}"
        )
    assert restored.status_code == 200
    assert restored.json()["state"] == "canceled"
    assert restored.json()["cancel_requested_at"] == canceled.json()[
        "cancel_requested_at"
    ]


@pytest.mark.parametrize(
    ("result_factory", "expected_state", "expected_finding"),
    [
        (_smartperfetto_result, "completed", None),
        (_live_smartperfetto_result, "partially_completed", "JIT 编译活跃"),
    ],
)
def test_local_app_accepts_a_trace_and_publishes_a_real_contract_report(
    tmp_path: Path,
    result_factory,
    expected_state: str,
    expected_finding: str | None,
) -> None:
    trace = b"local-perfetto-trace"
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")
    gateway = _FakeSmartPerfettoGateway(result_factory())
    app = create_local_app(
        gateway=gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf")
        assert csrf.status_code == 200
        csrf_token = csrf.json()["csrf_token"]
        headers = {"x-csrf-token": csrf_token}

        me = client.get("/v1/me")
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "ray_wu"
        team_id = me.json()["memberships"][0]["team"]["id"]

        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "trace_upload",
                "analysis_profile": "startup",
                "question": "首帧为什么慢？",
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
            },
        )
        assert created.status_code == 201
        analysis_id = created.json()["analysis_id"]

        reserved = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(trace),
                "sha256_b64": checksum,
            },
        )
        assert reserved.status_code == 201
        slot = reserved.json()["upload"]
        parsed_put = urlsplit(slot["put_url"])
        uploaded = client.put(
            f"{parsed_put.path}?{parsed_put.query}",
            content=trace,
            headers=slot["required_headers"],
        )
        assert uploaded.status_code == 200

        finalized = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(trace),
            },
        )
        assert finalized.status_code == 200

        terminal = None
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}",
                headers=headers,
            )
            if terminal.json()["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)

        assert terminal is not None
        assert terminal.status_code == 200
        assert terminal.json()["state"] == expected_state
        assert terminal.json()["report_available"] is True
        assert terminal.json()["ai_rounds"] == [
            {"round": 1, "role": "extract", "state": "completed", "attempts": 1},
            {"round": 2, "role": "review", "state": "completed", "attempts": 1},
            {"round": 3, "role": "finalize", "state": "completed", "attempts": 1},
        ]
        assert gateway.submissions == [(trace, "startup", "首帧为什么慢？")]

        report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report",
            headers=headers,
        )
        assert report.status_code == 200
        validated = validate_contract("analysis-report", report.json())
        assert validated["analysis_id"] == analysis_id
        assert validated["synthesis"]["state"] == "completed"
        assert validated["synthesis"]["output"]["recommendations"]
        if expected_finding is not None:
            bundle = validated["scenario_reports"][0]["bundle"]
            assert bundle is not None
            assert bundle["findings"][0]["title"] == expected_finding
            assert {metric["name"] for metric in bundle["metrics"]} >= {
                "startup.startup_analysis_get_startups.dur_ms",
                "startup.startup_analysis_get_startups.ttid_ms",
            }


def test_local_app_publishes_core_report_when_ai_projection_is_privacy_blocked(
    tmp_path: Path,
) -> None:
    result = _live_smartperfetto_result()
    report = result.payload["report"]
    assert isinstance(report, dict)
    contract = report["resultContract"]
    assert isinstance(contract, dict)
    diagnostics = contract["diagnostics"]
    assert isinstance(diagnostics, list)
    diagnostic = diagnostics[0]
    assert isinstance(diagnostic, dict)
    evidence = diagnostic["evidence"]
    assert isinstance(evidence, list)
    evidence[0] = {
        "text": "Trace evidence is stored at /Users/example/private/trace.pb"
    }
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(result),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client,
            team_id=team_id,
            headers=headers,
        )
        _upload_and_finalize_trace(
            client,
            team_id=team_id,
            analysis_id=analysis_id,
            headers=headers,
            checksum=checksum,
        )

        terminal = None
        for _ in range(200):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)

        assert terminal is not None
        assert terminal["state"] == "partially_completed"
        assert terminal["report_available"] is True
        assert terminal["stages"][-2]["state"] == "failed"
        assert terminal["stages"][-1]["state"] == "completed"
        published = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert published.status_code == 200
    validated = validate_contract("analysis-report", published.json())
    assert validated["synthesis"]["state"] == "failed"
    assert validated["synthesis"]["failure_code"] == "ai_projection_private_data"
    bundle = validated["scenario_reports"][0]["bundle"]
    assert bundle is not None
    assert bundle["findings"][0]["title"] == "JIT 编译活跃"
    projection = LocalAnalysisStore(tmp_path).load_document(
        UUID(analysis_id),
        "projection.json",
    )
    assert projection["question"] is None
    assert projection["scenarios"] == []
    assert "/Users/example/private/trace.pb" not in json.dumps(projection)


def test_local_app_restores_a_completed_report_after_restart(tmp_path: Path) -> None:
    trace = b"persistent-local-trace"
    checksum = base64.b64encode(hashlib.sha256(trace).digest()).decode("ascii")

    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
        csrf_token = client.get("/v1/auth/csrf").json()["csrf_token"]
        headers = {"x-csrf-token": csrf_token}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        created = client.post(
            f"/v1/teams/{team_id}/analyses",
            headers=headers,
            json={
                "schema_version": "1.0",
                "analysis_mode": "trace_upload",
                "analysis_profile": "startup",
                "question": None,
                "inputs": [
                    {
                        "kind": "trace",
                        "mime": "application/octet-stream",
                        "size": len(trace),
                        "sha256_b64": checksum,
                    }
                ],
            },
        ).json()
        analysis_id = created["analysis_id"]
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(trace),
                "sha256_b64": checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert client.put(
            f"{put.path}?{put.query}", content=trace, headers=slot["required_headers"]
        ).status_code == 200
        assert client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={"upload_id": slot["upload_id"], "sha256_b64": checksum, "size": len(trace)},
        ).status_code == 200
        for _ in range(100):
            state = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}").json()
            if state["report_available"]:
                break
            time.sleep(0.01)
        expected_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    store = LocalAnalysisStore(tmp_path)
    states = store.load_states()
    original_id = UUID(analysis_id)
    original_state = states[original_id]
    assert isinstance(original_state.get("created_at"), str)

    newer_id = UUID("92000000-0000-4000-8000-000000000002")
    newer_state = copy.deepcopy(original_state)
    newer_state["analysis_id"] = str(newer_id)
    newer_state["created_at"] = "2099-01-01T00:00:00+00:00"
    newer_report = copy.deepcopy(expected_report)
    newer_report["analysis_id"] = str(newer_id)
    store.save_state(newer_id, newer_state)
    store.save_document(newer_id, "report.json", newer_report)

    pending_id = UUID("92000000-0000-4000-8000-000000000003")
    pending_state = copy.deepcopy(original_state)
    pending_state["analysis_id"] = str(pending_id)
    pending_state["created_at"] = "2100-01-01T00:00:00+00:00"
    pending_state["report_available"] = False
    store.save_state(pending_id, pending_state)

    legacy_state = copy.deepcopy(original_state)
    legacy_state.pop("created_at")
    store.save_state(original_id, legacy_state)

    second_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")
        assert restored.status_code == 200
        assert restored.json()["report_available"] is True
        assert datetime.fromisoformat(restored.json()["created_at"]) == datetime.fromisoformat(
            expected_report["generated_at"].replace("Z", "+00:00")
        )
        assert client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json() == expected_report
        latest = client.get(
            f"/v1/teams/{team_id}/analyses?report_available=true&limit=1"
        )
        assert latest.status_code == 200
        assert latest.json()["schema_version"] == "1.0"
        assert [item["analysis_id"] for item in latest.json()["analyses"]] == [
            str(newer_id)
        ]
        assert all(item["report_available"] for item in latest.json()["analyses"])


def test_local_recovery_imports_a_completed_smartperfetto_session_once(
    tmp_path: Path,
) -> None:
    result = _live_smartperfetto_result()
    report = result.payload["report"]
    assert isinstance(report, dict)
    summary = report["summary"]
    assert isinstance(summary, dict)
    summary["rounds"] = 53
    report["claimVerificationResult"] = {"status": "passed"}
    gateway = _FakeSmartPerfettoGateway(result)
    app = create_local_app(
        gateway=gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    checksum = base64.b64encode(hashlib.sha256(b"recovered-trace").digest()).decode(
        "ascii"
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        payload = {
            "schema_version": "1.0",
            "session_id": "session-local-1",
            "run_id": "run-local-1",
            "analysis_profile": "auto",
            "question": "给出最终优化方案",
            "trace_size": len(b"recovered-trace"),
            "trace_sha256_b64": checksum,
        }
        first = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=payload,
        )
        second = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=payload,
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["analysis_id"] == first.json()["analysis_id"]
        analysis_id = first.json()["analysis_id"]
        terminal = first.json()
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["report_available"]:
                break
            time.sleep(0.01)

        assert terminal["source_analysis"] == {
            "engine": "smartperfetto",
            "rounds": 53,
            "verification": "passed",
            "session_id": "session-local-1",
            "run_id": "run-local-1",
        }
        assert terminal["ai_rounds"] == [
            {"round": 1, "role": "extract", "state": "completed", "attempts": 1},
            {"round": 2, "role": "review", "state": "completed", "attempts": 1},
            {"round": 3, "role": "finalize", "state": "completed", "attempts": 1},
        ]
        assert gateway.submissions == []

        completed_version = terminal["version"]
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 2
        for _ in range(100):
            rerun_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if (
                rerun_state["version"] > completed_version
                and rerun_state["stages"][2]["state"] == "completed"
                and rerun_state["stages"][3]["state"] == "completed"
            ):
                break
            time.sleep(0.01)
        assert rerun_state["ai_rounds"][2]["state"] == "completed"
