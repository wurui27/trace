from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from perfpilot_agent.capture import CaptureTaskRunner, ThermalReading
from perfpilot_agent.config import AgentConfig
from perfpilot_agent.control_client import (
    ControlClient,
    SourceTaskExecuteResponse,
    TaskExecuteResponse,
)
from perfpilot_agent.credentials import AgentCredentials, TaskSigningKey
from perfpilot_agent.executor import TaskExecutor
from perfpilot_agent.security import TaskVerifier
from perfpilot_agent.service import TaskLoop
from perfpilot_agent.state import AgentRuntimeState, DeviceBinding
from perfpilot_agent.uploads import InputDownloader, MultipartUploader
from perfpilot_api.ai.local_report import LocalReportSynthesizer
from perfpilot_api.ai.openai_compatible import SynthesisCandidate
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.local_app import LocalEngineRun, create_local_app
from perfpilot_api.local_control_store import LocalControlStore
from perfpilot_api.local_device_capture import LocalApkMetadata
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.security.agent_signatures import encode_ed25519_public_key


ROOT = Path(__file__).resolve().parents[4]
TEAM_ONE_SERIAL = "TEAM-ONE-PRIVATE-SERIAL"
TEAM_TWO_SERIAL = "TEAM-TWO-PRIVATE-SERIAL"
TEAM_ONE_WORKSPACE = UUID("92000000-0000-4000-8000-000000000001")
TEAM_TWO_WORKSPACE = UUID("92000000-0000-4000-8000-000000000002")
TEAM_ONE_PROFILE = UUID("96000000-0000-4000-8000-000000000001")
TEAM_TWO_PROFILE = UUID("96000000-0000-4000-8000-000000000002")


def _sha(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _smartperfetto_result() -> EngineResult:
    fixture = ROOT / (
        "services/api/tests/fixtures/canonical_results/"
        "smartperfetto-result-contract-1.0.0.json"
    )
    canonical = json.loads(fixture.read_text(encoding="utf-8"))
    payload = canonical["result"]["payload"]
    for private_key in ("actions", "workspaceId", "runId"):
        payload["report"].pop(private_key, None)
    payload["report"]["reportId"] = payload["reportId"]
    payload["report"]["dataEnvelopes"][0]["evidence"][0]["fields"][
        "mapped_symbol"
    ] = "demo.Startup.init"
    return EngineResult(
        contract="workspace-agent-v1",
        state="completed",
        payload=payload,
    )


class _ScenarioSmartPerfetto:
    def __init__(self) -> None:
        self.result = _smartperfetto_result()
        self.submissions: list[tuple[bytes, str, str | None]] = []
        self._runs: dict[str, tuple[str, bytes]] = {}

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        trace = trace_path.read_bytes()
        self.submissions.append((trace, profile, question))
        run_id = f"run-{len(self.submissions)}-{profile}"
        self._runs[run_id] = (profile, trace)
        return LocalEngineRun(session_id=f"session-{run_id}", run_id=run_id)

    async def status(self, run: LocalEngineRun) -> str:
        assert run.run_id in self._runs
        return "completed"

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        profile, trace = self._runs[run.run_id]
        original = canonical_json_bytes(
            {
                "scenario": profile,
                "trace_sha256": hashlib.sha256(trace).hexdigest(),
            }
        )
        return EngineResult(
            contract=self.result.contract,
            state=self.result.state,
            payload=self.result.payload,
            original_report_bytes=original,
        )

    async def cancel(self, run: LocalEngineRun) -> None:
        raise AssertionError(f"unexpected SmartPerfetto cancellation: {run.run_id}")

    async def aclose(self) -> None:
        return None


class _OneRoundChineseProvider:
    provider_name = "acceptance-provider"
    model = "acceptance-model"
    prompt_version = "perfpilot-report-v3-acceptance"
    prompt_sha256_b64 = _sha(b"remote-agent-acceptance-prompt")

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        projected = projection.document
        findings = []
        recommendations = []
        retest_plan = []
        key_metric_ids = []
        for scenario in projected["scenarios"]:
            for finding in scenario["findings"]:
                evidence_ids = list(finding["evidence_ids"])
                findings.append(
                    {
                        "finding_id": finding["finding_id"],
                        "evidence_ids": evidence_ids,
                        "user_impact": "该问题会延迟用户看到可交互界面的时间。",
                    }
                )
                if finding["status"] in {"confirmed", "suspected"} and evidence_ids:
                    recommendations.append(
                        {
                            "priority": ("p0", "p1", "p2")[
                                min(len(recommendations), 2)
                            ],
                            "title": "处理已确认的性能瓶颈",
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
                key_metric_ids.extend(metric_ids)
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
            "schema_version": "2.0",
            "verdict": "存在证据支持的应用性能瓶颈。",
            "executive_summary": "单次 PerfPilot AI 已完成中文证据复核。",
            "key_metric_ids": key_metric_ids[:3],
            "top_findings": findings[:3],
            "recommendations": recommendations[:3],
            "source_fixes": [],
            "retest_plan": retest_plan[:3],
            "limitations": [
                {
                    "limitation_id": item["limitation_id"],
                    "summary": "当前证据存在已标记的覆盖限制。",
                }
                for item in projected["limitations"]
            ],
        }
        source_context = projected.get("source_context")
        if (
            isinstance(source_context, dict)
            and source_context.get("match_summary") == "strong"
            and source_context.get("fragments")
            and recommendations
        ):
            source_ref = source_context["fragments"][0]
            path = source_ref["relative_path"]
            document["source_fixes"] = [
                {
                    "fix_id": "95000000-0000-4000-8000-000000000001",
                    "finding_id": source_ref["finding_ids"][0],
                    "evidence_ids": source_ref["evidence_ids"],
                    "recommendation_priority": recommendations[0]["priority"],
                    "source_ref_ids": [source_ref["source_ref_id"]],
                    "rule_id": source_ref["rule_ids"][0],
                    "match_grade": "strong",
                    "relative_path": path,
                    "symbol": source_ref["symbol"],
                    "diagnosis": "启动路径在主线程执行了可延迟初始化。",
                    "diff": (
                        f"diff --git a/{path} b/{path}\n"
                        f"--- a/{path}\n"
                        f"+++ b/{path}\n"
                        "@@ -1 +1 @@\n"
                        "-fun init() = loadNow()\n"
                        "+fun init() = loadLazily()\n"
                    ),
                    "validation_profile_id": str(TEAM_ONE_PROFILE),
                    "retest_target": "重复冷启动并对比首帧耗时。",
                }
            ]
        return SynthesisCandidate(
            candidate_json=canonical_json_bytes(document),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=5,
        )

    async def aclose(self) -> None:
        return None


class _InjectedAapt2Inspector:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def inspect(self, apk_path: Path) -> LocalApkMetadata:
        self.calls.append(apk_path.read_bytes())
        return LocalApkMetadata(
            package_name="com.example.perfpilot",
            version_name="1.2.3",
            version_code=123,
            launch_activity="com.example.perfpilot/com.example.perfpilot.MainActivity",
            min_sdk=26,
            target_sdk=35,
            supported_abis=("arm64-v8a",),
            has_native_libraries=True,
        )


class _ForbiddenHostProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def inspect(self):
        self.calls += 1
        raise AssertionError("remote flow must not probe server-host ADB")


@dataclass
class _FakeCaptureDevice:
    trace_prefix: str

    def __post_init__(self) -> None:
        self.installed: list[bytes] = []
        self.captured: list[str] = []
        self.cleaned = 0

    async def adb_version(self) -> str:
        return "Android Debug Bridge version 1.0.41"

    async def thermal_reading(self) -> ThermalReading:
        return ThermalReading(temperature_c=31.5, thermal_status=0)

    async def install(self, apk: Path) -> None:
        self.installed.append(apk.read_bytes())

    async def capture_trace(
        self, *, scenario_type: str, output: Path, **_kwargs: object
    ) -> None:
        self.captured.append(scenario_type)
        output.write_bytes(f"{self.trace_prefix}-{scenario_type}-trace".encode())

    async def collect_memory_samples(self, **_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("remote trace capture must not request memory")

    async def cleanup(self) -> None:
        self.cleaned += 1

    async def uninstall(self, package_name: str) -> None:
        assert package_name == "com.example.perfpilot"


def _browser_login(client: TestClient, username: str, password: str) -> dict[str, str]:
    csrf = client.get("/v1/auth/csrf")
    assert csrf.status_code == 200
    response = client.post(
        "/v1/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "x-csrf-token": csrf.json()["csrf_token"],
        },
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {
        "Origin": "http://localhost:3000",
        "x-csrf-token": response.json()["csrf_token"],
    }


def _register_agent(
    client: TestClient,
    *,
    team_id: UUID,
    browser_headers: dict[str, str],
    private_key: Ed25519PrivateKey,
    serial: str,
    workspace_id: UUID,
    profile_id: UUID,
) -> tuple[dict[str, object], dict[str, object]]:
    registration = client.post(
        f"/v1/teams/{team_id}/agents/registration-codes",
        headers=browser_headers,
        json={"schema_version": "1.0", "name": f"Agent {serial[-3:]}"},
    )
    assert registration.status_code == 201, registration.text
    credentials = client.post(
        "/v1/agent/register",
        json={
            "schema_version": "1.1",
            "registration_code": registration.json()["registration_code"],
            "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
            "platform": "macos",
            "agent_version": "1.2.3",
            "hostname": f"capture-{serial[-3:].lower()}",
            "os_version": "macOS 15",
        },
    )
    assert credentials.status_code == 201, credentials.text
    document = credentials.json()
    heartbeat = client.post(
        "/v1/agent/heartbeat",
        headers={"Authorization": f"Bearer {document['access_token']}"},
        json={
            "schema_version": "1.1",
            "agent_version": "1.2.3",
            "platform": "macos",
            "hostname": f"capture-{serial[-3:].lower()}",
            "observed_at": datetime.now().astimezone().isoformat(),
            "clock_skew_ms": 0,
            "disk_available_bytes": 1024,
            "execution_slot": {"state": "idle", "execution_id": None},
            "devices": [
                {
                    "client_ref": str(UUID(int=workspace_id.int + 100)),
                    "serial": serial,
                    "manufacturer": "Google",
                    "model": "Pixel",
                    "android_release": "16",
                    "api_level": 36,
                    "connection_type": "usb",
                    "adb_state": "device",
                    "battery_percent": 80,
                    "temperature_c": None,
                    "storage_available_bytes": 1024,
                    "property_error_code": None,
                }
            ],
            "workspaces": [
                {
                    "workspace_id": str(workspace_id),
                    "name": f"Workspace {serial[-3:]}",
                    "state": "ready",
                    "git_branch": "main",
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 0,
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [
                        {"profile_id": str(profile_id), "name": "Unit tests"}
                    ],
                }
            ],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return document, heartbeat.json()["devices"][0]


def _create_and_finalize(
    client: TestClient,
    *,
    team_id: UUID,
    browser_headers: dict[str, str],
    credentials: dict[str, object],
    device: dict[str, object],
    workspace_id: UUID,
    profile_id: UUID,
    apk: bytes,
) -> dict[str, object]:
    created = client.post(
        f"/v1/teams/{team_id}/analyses",
        headers=browser_headers,
        json={
            "schema_version": "1.1",
            "analysis_mode": "device",
            "device_id": device["device_id"],
            "scenarios": ["cold_start", "scroll", "memory_cycle"],
            "apk": {
                "artifact_kind": "apk",
                "mime": "application/vnd.android.package-archive",
                "size": len(apk),
                "sha256_b64": _sha(apk),
            },
            "source_binding": {
                "provider_kind": "agent_workspace",
                "agent_id": credentials["agent_id"],
                "workspace_id": str(workspace_id),
                "snapshot_policy": "tracked_worktree",
                "validation_profile_id": str(profile_id),
            },
        },
    )
    assert created.status_code == 201, created.text
    document = created.json()
    slot = document["apk_upload"]
    put = urlsplit(slot["put_url"])
    uploaded = client.put(
        f"{put.path}?{put.query}",
        content=apk,
        headers=slot["required_headers"],
    )
    assert uploaded.status_code == 200, uploaded.text
    finalized = client.post(
        f"/v1/teams/{team_id}/analyses/{document['analysis_id']}/finalize-upload",
        headers=browser_headers,
        json={
            "upload_id": slot["upload_id"],
            "sha256_b64": _sha(apk),
            "size": len(apk),
        },
    )
    assert finalized.status_code == 200, finalized.text
    return document


def _bound_credentials(
    credentials: dict[str, object],
    private_key: Ed25519PrivateKey,
    team_id: UUID,
) -> AgentCredentials:
    return AgentCredentials(
        schema_version="1.1",
        agent_id=UUID(str(credentials["agent_id"])),
        team_id=team_id,
        private_key_b64=base64.b64encode(
            private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode("ascii"),
        access_token=str(credentials["access_token"]),
        access_token_expires_at=datetime.fromisoformat(
            str(credentials["access_token_expires_at"])
        ),
        refresh_token=str(credentials["refresh_token"]),
        refresh_token_expires_at=datetime.fromisoformat(
            str(credentials["refresh_token_expires_at"])
        ),
        task_signing_key=TaskSigningKey.model_validate(
            credentials["task_signing_key"]
        ),
        heartbeat_interval_seconds=10,
    )


async def _run_signed_agent(
    *,
    app,
    tmp_path: Path,
    credentials: dict[str, object],
    private_key: Ed25519PrivateKey,
    team_id: UUID,
    device: dict[str, object],
    serial: str,
    analysis_id: UUID,
) -> _FakeCaptureDevice:
    ca = tmp_path / f"{team_id}-ca.crt"
    ca.write_text("test", encoding="utf-8")
    config = AgentConfig(
        server_url="https://testserver",
        ca_bundle=ca,
        workspace_root=tmp_path / f"agent-work-{team_id}",
    )
    bound = _bound_credentials(credentials, private_key, team_id)
    state = AgentRuntimeState()
    state.replace_device_bindings(
        (
            DeviceBinding(
                client_ref=UUID(int=TEAM_ONE_WORKSPACE.int + 100),
                device_id=UUID(str(device["device_id"])),
                device_digest=str(device["device_digest"]),
                serial=serial,
            ),
        )
    )
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    )
    control = ControlClient(config, http_client=http_client, credentials=bound)
    fake_device = _FakeCaptureDevice(trace_prefix="team-one")
    runner = CaptureTaskRunner(
        config=config,
        adb_binary=tmp_path / "agent-adb",
        control=control,
        state=state,
        redactor=None,
        device_factory=lambda **_kwargs: fake_device,
        downloader_factory=lambda **kwargs: InputDownloader(
            control=control,
            http_client=http_client,
            **kwargs,
        ),
        uploader_factory=lambda **kwargs: MultipartUploader(
            control=control,
            http_client=http_client,
            **kwargs,
        ),
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    loop = TaskLoop(
        control=control,
        executor=TaskExecutor(
            control=control,
            runner=runner,
            state=state,
            control_poll_interval_seconds=0.01,
            renewal_interval_seconds=20,
        ),
        state=state,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    try:
        assert await loop.poll_once() is True
        source_delivery = None
        for _ in range(200):
            candidate = await control.poll_task(wait_seconds=0)
            if isinstance(candidate, SourceTaskExecuteResponse):
                source_delivery = candidate
                break
            await asyncio.sleep(0.01)
        assert source_delivery is not None
        snapshot = source_delivery.snapshot
        hints = snapshot["finding_hints"]
        assert isinstance(hints, list) and hints
        hint = hints[0]
        assert isinstance(hint, dict)
        content = "fun init() = loadNow()"
        await control.complete_source_task(
            execution_id=UUID(str(snapshot["execution_id"])),
            lease_version=int(snapshot["lease_version"]),
            lease_token=source_delivery.lease_token,
            completion={
                "schema_version": "1.0",
                "task_type": "source_context",
                "execution_id": str(snapshot["execution_id"]),
                "analysis_id": str(snapshot["analysis_id"]),
                "workspace_id": str(snapshot["workspace_id"]),
                "lease_version": int(snapshot["lease_version"]),
                "state": "completed",
                "result": {
                    "snapshot_id": "94000000-0000-4000-8000-000000000001",
                    "snapshot_hash": "b" * 64,
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 0,
                    "fragments": [
                        {
                            "source_ref_id": "97000000-0000-4000-8000-000000000001",
                            "relative_path": "app/src/main/java/demo/Startup.kt",
                            "language": "kotlin",
                            "symbol": "demo.Startup.init",
                            "start_line": 1,
                            "end_line": 1,
                            "content": content,
                            "content_sha256": hashlib.sha256(
                                content.encode()
                            ).hexdigest(),
                            "snapshot_hash": "b" * 64,
                            "finding_ids": [hint["finding_id"]],
                            "evidence_ids": hint["evidence_ids"],
                            "rule_ids": ["android.startup.eager_initialization"],
                            "match_signals": ["trace_symbol"],
                        }
                    ],
                    "exclusions": [],
                    "truncated": False,
                },
            },
        )
        analysis = app.state.local_runtime.analyses[(team_id, analysis_id)]
        assert analysis.task is not None
        await asyncio.wait_for(asyncio.shield(analysis.task), timeout=3)
    finally:
        await control.aclose()
        await http_client.aclose()
    return fake_device


def _assert_redacted(response, secrets: tuple[str, ...]) -> None:
    assert response.status_code in {403, 404}
    body = response.text
    assert not re.search(r"(?:/Users/|/tmp/|\\\\|ppat_|ppreg_|pprt_)", body)
    assert all(secret not in body for secret in secrets)


def test_remote_agent_full_success_is_isolated_across_two_local_tenants(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    user_one = control.ensure_user("team_one", "initial password one", False).principal
    user_two = control.ensure_user("team_two", "initial password two", False).principal
    control.change_password(
        user_one.user_id, "initial password one", "established password one"
    )
    control.change_password(
        user_two.user_id, "initial password two", "established password two"
    )
    gateway = _ScenarioSmartPerfetto()
    provider = _OneRoundChineseProvider()
    inspector = _InjectedAapt2Inspector()
    host_probe = _ForbiddenHostProbe()
    data_root = tmp_path / "data"
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=data_root,
        state_root=tmp_path / "state",
        control_store=control,
        apk_inspector=inspector,
        device_probe=host_probe,
        public_origin="https://testserver",
        source_code_analysis_enabled=True,
    )
    key_one = Ed25519PrivateKey.generate()
    key_two = Ed25519PrivateKey.generate()
    apk = b"team-one-private-apk"

    with TestClient(app) as client:
        headers_one = _browser_login(
            client, "team_one", "established password one"
        )
        credentials_one, device_one = _register_agent(
            client,
            team_id=user_one.team_id,
            browser_headers=headers_one,
            private_key=key_one,
            serial=TEAM_ONE_SERIAL,
            workspace_id=TEAM_ONE_WORKSPACE,
            profile_id=TEAM_ONE_PROFILE,
        )
        created = _create_and_finalize(
            client,
            team_id=user_one.team_id,
            browser_headers=headers_one,
            credentials=credentials_one,
            device=device_one,
            workspace_id=TEAM_ONE_WORKSPACE,
            profile_id=TEAM_ONE_PROFILE,
            apk=apk,
        )
        client.cookies.clear()
        headers_two = _browser_login(
            client, "team_two", "established password two"
        )
        credentials_two, device_two = _register_agent(
            client,
            team_id=user_two.team_id,
            browser_headers=headers_two,
            private_key=key_two,
            serial=TEAM_TWO_SERIAL,
            workspace_id=TEAM_TWO_WORKSPACE,
            profile_id=TEAM_TWO_PROFILE,
        )

        own_devices = client.get(f"/v1/teams/{user_two.team_id}/devices")
        own_workspaces = client.get(f"/v1/teams/{user_two.team_id}/source-workspaces")
        assert own_devices.status_code == 200, own_devices.text
        assert own_workspaces.status_code == 200, own_workspaces.text
        assert [item["device_id"] for item in own_devices.json()["devices"]] == [
            device_two["device_id"]
        ]
        assert [
            item["workspace_id"] for item in own_workspaces.json()["workspaces"]
        ] == [str(TEAM_TWO_WORKSPACE)]
        assert TEAM_ONE_SERIAL not in own_devices.text
        assert str(TEAM_ONE_WORKSPACE) not in own_workspaces.text

        client.cookies.clear()
        team_two_poll = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={
                "Authorization": f"Bearer {credentials_two['access_token']}"
            },
        )
        assert team_two_poll.status_code == 200
        assert team_two_poll.json()["action"] == "wait"

        team_one_delivery = client.get(
            "/v1/agent/tasks/next?wait_seconds=0",
            headers={
                "Authorization": f"Bearer {credentials_one['access_token']}"
            },
        )
        assert team_one_delivery.status_code == 200, team_one_delivery.text
        delivery = TaskExecuteResponse.model_validate(team_one_delivery.json())
        task = TaskVerifier(
            public_key_b64=credentials_one["task_signing_key"]["public_key_b64"],
            kid=credentials_one["task_signing_key"]["kid"],
        ).verify(
            delivery.snapshot_jws,
            expected_agent_id=UUID(str(credentials_one["agent_id"])),
            expected_team_id=user_one.team_id,
            expected_lease_version=None,
            known_device_digests={str(device_one["device_digest"])},
        )
        stolen_input = client.post(
            f"/v1/agent/tasks/{task.execution_id}/inputs/"
            f"{task.input_artifacts[0].artifact_id}",
            headers={
                "Authorization": f"Bearer {credentials_two['access_token']}"
            },
            json={"schema_version": "1.0", "lease_version": task.lease_version},
        )
        now = datetime.now(UTC).isoformat()
        fake_artifact = "76000000-0000-4000-8000-000000000099"
        stolen_completion = client.post(
            f"/v1/agent/tasks/{task.execution_id}/complete",
            headers={
                "Authorization": f"Bearer {credentials_two['access_token']}"
            },
            json={
                "schema_version": "1.0",
                "execution_id": str(task.execution_id),
                "lease_version": task.lease_version,
                "state": "failed",
                "started_at": now,
                "completed_at": now,
                "agent_version": "1.2.3",
                "adb_version": "Android Debug Bridge version 1.0.41",
                "artifacts": [
                    {
                        "artifact_id": fake_artifact,
                        "kind": "agent_log",
                        "mime": "text/plain",
                        "size": 1,
                        "sha256_b64": _sha(b"x"),
                    }
                ],
                "scenarios": [
                    {
                        "scenario_type": "startup",
                        "state": "failed",
                        "started_at": now,
                        "completed_at": now,
                        "temperature_start_c": None,
                        "temperature_end_c": None,
                        "artifact_ids": [],
                        "diagnostic_code": "trace_capture_failed",
                    }
                ],
                "diagnostic_code": "capture_failed",
            },
        )
        leaked = (
            TEAM_ONE_SERIAL,
            str(user_one.team_id),
            str(task.input_artifacts[0].artifact_id),
            str(data_root),
            str(credentials_one["access_token"]),
        )
        _assert_redacted(stolen_input, leaked)
        _assert_redacted(stolen_completion, leaked)

        client.cookies.clear()
        _browser_login(client, "team_one", "established password one")
        captured = asyncio.run(
            _run_signed_agent(
                app=app,
                tmp_path=tmp_path,
                credentials=credentials_one,
                private_key=key_one,
                team_id=user_one.team_id,
                device=device_one,
                serial=TEAM_ONE_SERIAL,
                analysis_id=UUID(str(created["analysis_id"])),
            )
        )
        analysis = client.get(
            f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}"
        )
        report_response = client.get(
            f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}/report"
        )
        originals_response = client.get(
            f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}"
            "/smartperfetto-original"
        )
        downloads = {
            scenario: client.get(
                f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}"
                f"/smartperfetto-original?scenario={scenario}&download=true"
            )
            for scenario in ("startup", "scroll")
        }
        manifest = app.state.local_runtime.store.load_document(
            user_one.team_id,
            UUID(str(created["analysis_id"])),
            "agent-capture-manifest.json",
        )

        client.cookies.clear()
        _browser_login(client, "team_two", "established password two")
        cross_responses = (
            client.get(
                f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}"
            ),
            client.get(
                f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}/report"
            ),
            client.get(
                f"/v1/teams/{user_one.team_id}/analyses/{created['analysis_id']}"
                "/smartperfetto-original?scenario=startup&download=true"
            ),
        )

    assert inspector.calls == [apk]
    assert host_probe.calls == 0
    assert captured.installed == [apk]
    assert captured.captured == ["startup", "scroll"]
    assert captured.cleaned >= 1
    assert analysis.status_code == 200, analysis.text
    projected = analysis.json()
    assert projected["state"] == "completed"
    assert [item["state"] for item in projected["scenarios"]] == [
        "completed",
        "completed",
        "not_requested",
    ]
    assert gateway.submissions == [
        (b"team-one-startup-trace", "startup", None),
        (b"team-one-scroll-trace", "scroll", None),
    ]
    assert provider.calls == 1
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["schema_version"] == "1.2"
    assert report["report_version"] == 1
    assert [item["scenario_type"] for item in report["scenario_reports"]] == [
        "startup",
        "scroll",
    ]
    output = report["synthesis"]["output"]
    assert re.search(r"[\u4e00-\u9fff]", output["verdict"])
    assert report["source_code"]["context_state"] == "available"
    assert report["source_code"]["match_summary"] == "strong"
    fix = report["source_code"]["fixes"][0]
    assert fix["relative_path"] == "app/src/main/java/demo/Startup.kt"
    assert fix["symbol"] == "demo.Startup.init"
    assert fix["diff"].startswith(
        "diff --git a/app/src/main/java/demo/Startup.kt "
        "b/app/src/main/java/demo/Startup.kt\n"
    )
    assert fix["retest_target"] == "重复冷启动并对比首帧耗时。"

    assert manifest is not None
    assert [item["kind"] for item in manifest["artifacts"]] == [
        "startup_trace",
        "scroll_trace",
        "agent_log",
    ]
    artifact_root = (
        data_root
        / "teams"
        / str(user_one.team_id)
        / "analyses"
        / str(created["analysis_id"])
        / "agent-artifacts"
        / "completed"
    )
    log = next(artifact_root.glob("agent_log-*.bin")).read_text(encoding="utf-8")
    assert "schema_version=1.0" in log
    assert "scenario=startup state=completed" in log
    assert "scenario=scroll state=completed" in log

    assert originals_response.status_code == 200, originals_response.text
    assert [
        item["scenario_type"] for item in originals_response.json()["reports"]
    ] == ["startup", "scroll"]
    assert downloads["startup"].content == canonical_json_bytes(
        {
            "scenario": "startup",
            "trace_sha256": hashlib.sha256(
                b"team-one-startup-trace"
            ).hexdigest(),
        }
    )
    assert downloads["scroll"].content == canonical_json_bytes(
        {
            "scenario": "scroll",
            "trace_sha256": hashlib.sha256(b"team-one-scroll-trace").hexdigest(),
        }
    )
    cross_secrets = (
        TEAM_ONE_SERIAL,
        str(user_one.team_id),
        str(created["analysis_id"]),
        str(data_root),
        str(credentials_one["access_token"]),
    )
    for response in cross_responses:
        _assert_redacted(response, cross_secrets)


@pytest.mark.parametrize(
    "origin",
    ["https://public.example.test", "http://127.0.0.1:3000"],
)
def test_remote_agent_runtime_accepts_only_https_or_private_http_origins(
    origin: str,
) -> None:
    # The end-to-end flow above uses HTTPS; this keeps the accepted private HTTP
    # deployment seam explicit without duplicating that lifecycle fixture.
    from perfpilot_api.local_app import _public_origin

    assert _public_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    ["http://public.example.test", "https://user:password@example.test"],
)
def test_remote_agent_runtime_rejects_public_http_or_credentialed_origins(
    origin: str,
) -> None:
    from perfpilot_api.local_app import _public_origin

    with pytest.raises(ValueError):
        _public_origin(origin)
