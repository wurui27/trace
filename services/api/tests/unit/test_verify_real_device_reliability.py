from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "verify-real-device-reliability.py"


def _module():
    spec = importlib.util.spec_from_file_location("verify_real_device_reliability", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.created: list[dict[str, object]] = []
        self.analysis_reads = 0
        self.paths: list[str] = []

    def me(self) -> dict[str, object]:
        return {
            "memberships": [
                {"team": {"id": "11000000-0000-4000-8000-000000000001"}}
            ],
            "private_path": "/Users/private/control.json",
        }

    def agents(self, team_id: str) -> dict[str, object]:
        self.paths.append(f"teams/{team_id}/agents")
        return {
            "agents": [
                {
                    "agent_id": "71000000-0000-4000-8000-000000000001",
                    "state": "online",
                    "token": "agent-secret",
                }
            ]
        }

    def devices(self, team_id: str) -> dict[str, object]:
        self.paths.append(f"teams/{team_id}/devices")
        return {
            "devices": [
                {
                    "device_id": "72000000-0000-4000-8000-000000000001",
                    "agent_id": "71000000-0000-4000-8000-000000000001",
                    "state": "ready" if self.ready else "offline",
                    "launch_targets": [
                        {
                            "package_name": "com.rivotek.mediacenter",
                            "launch_activity": (
                                "com.rivotek.mediacenter/mediacenteractivity"
                            ),
                        }
                    ],
                }
            ]
        }

    def workspaces(self, team_id: str) -> dict[str, object]:
        self.paths.append(f"teams/{team_id}/source-workspaces")
        return {
            "workspaces": [
                {
                    "workspace_id": "71000000-0000-4000-8000-000000000001",
                    "agent_id": "71000000-0000-4000-8000-000000000001",
                    "state": "ready",
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [],
                }
            ]
        }

    def create_analysis(self, team_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.paths.append(f"teams/{team_id}/analyses")
        self.created.append(payload)
        return {
            "schema_version": "1.3",
            "analysis_id": "73000000-0000-4000-8000-000000000001",
            "state": "queued",
        }

    def analysis(self, team_id: str, analysis_id: str) -> dict[str, object]:
        self.paths.append(f"teams/{team_id}/analyses/{analysis_id}")
        stages = ["smartperfetto", "source_code", "perfpilot_ai", "report"]
        stage = stages[min(self.analysis_reads, len(stages) - 1)]
        self.analysis_reads += 1
        terminal = stage == "report"
        return {
            "schema_version": "1.3",
            "analysis_id": analysis_id,
            "state": "completed" if terminal else "analyzing",
            "runtime_status": {
                "current_stage": stage,
                "stage_state": "completed" if terminal else "running",
                "progress_summary": "正在处理真实设备证据",
                "updated_at": "2026-08-20T12:00:00+00:00",
            },
        }

    def report(self, team_id: str, analysis_id: str) -> dict[str, object]:
        self.paths.append(f"teams/{team_id}/analyses/{analysis_id}/report")
        return {
            "schema_version": "1.2",
            "synthesis": {
                "state": "completed",
                "provenance": {"generation": 1},
                "output": {
                    "verdict": "启动阶段存在可以优化的主线程阻塞。",
                    "executive_summary": "建议延后非关键初始化并按相同场景复测。",
                },
            },
            "source_code": {
                "context_state": "available",
                "match_summary": "strong",
                "source_refs": [{"source_ref_id": "ref-1"}],
            },
            "provider_token": "must-not-leak",
        }

    def original_html(self, team_id: str, analysis_id: str) -> bytes:
        self.paths.append(
            f"teams/{team_id}/analyses/{analysis_id}/smartperfetto-original"
        )
        return b"<!doctype html><html><body>SmartPerfetto</body></html>"


def _config(module):
    return module.VerificationConfig(
        server_url="https://server.example",
        package="com.rivotek.mediacenter",
        activity="mediacenteractivity",
        test_type="cold_start",
        duration_seconds=15,
        source_workspace_id="71000000-0000-4000-8000-000000000001",
        poll_interval_seconds=0.01,
    )


def test_parse_arguments_matches_the_documented_real_device_command() -> None:
    module = _module()
    parsed = module.parse_args(
        [
            "--server-url",
            "https://server.example",
            "--package",
            "com.rivotek.mediacenter",
            "--activity",
            "mediacenteractivity",
            "--test-type",
            "cold_start",
            "--duration-seconds",
            "15",
            "--source-workspace-id",
            "71000000-0000-4000-8000-000000000001",
        ]
    )

    assert parsed.package == "com.rivotek.mediacenter"
    assert parsed.duration_seconds == 15


def test_success_summary_is_safe_and_uses_remote_agent_only() -> None:
    module = _module()
    client = FakeClient()
    emitted: list[str] = []

    summary = module.verify_reliability(
        client,
        _config(module),
        sleep=lambda _seconds: None,
        emit=emitted.append,
    )

    assert client.created == [
        {
            "schema_version": "1.3",
            "analysis_mode": "device",
            "device_id": "72000000-0000-4000-8000-000000000001",
            "test_type": "cold_start",
            "launch_mode": "automatic",
            "duration_seconds": 15,
            "target": {
                "package_name": "com.rivotek.mediacenter",
                "launch_activity": "com.rivotek.mediacenter/mediacenteractivity",
            },
            "source_binding": {
                "provider_kind": "agent_workspace",
                "agent_id": "71000000-0000-4000-8000-000000000001",
                "workspace_id": "71000000-0000-4000-8000-000000000001",
                "snapshot_policy": "tracked_worktree",
                "validation_profile_id": None,
            },
        }
    ]
    assert all(path != "device" for path in client.paths)
    assert summary["result"] == "passed"
    assert summary["state"] == "completed"
    assert summary["source_match"] == "strong"
    assert summary["original_html_verified"] is True
    assert summary["observed_stages"] == [
        "smartperfetto",
        "source_code",
        "perfpilot_ai",
        "report",
    ]
    encoded = json.dumps(summary, ensure_ascii=False)
    assert "/Users/" not in encoded
    assert "token" not in encoded.lower()
    assert "源码中的具体内容" not in encoded
    assert any("SmartPerfetto" in item for item in emitted)


def test_missing_ready_device_returns_a_stable_failure_code() -> None:
    module = _module()

    with pytest.raises(module.VerificationFailure) as error:
        module.verify_reliability(
            FakeClient(ready=False),
            _config(module),
            sleep=lambda _seconds: None,
            emit=lambda _message: None,
        )

    assert error.value.code == "ready_device_unavailable"
    assert "/Users/" not in str(error.value)
