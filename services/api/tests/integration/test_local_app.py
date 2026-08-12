from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient as _RawTestClient

from perfpilot_api.ai.local_report import LocalReportSynthesizer
from perfpilot_api.ai.openai_compatible import SynthesisCandidate
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.local_app import (
    LocalEngineRun,
    _evidence_manifest,
    _prepare_local_report,
    _public_origin,
    _restore_ai_rounds,
    _source_code_analysis_unavailable_document,
    create_local_app,
)
from perfpilot_api.local_analysis_store import (
    LocalAnalysisStore,
    LocalAnalysisStoreDurabilityError,
    LocalAnalysisStoreError,
)
from perfpilot_api.local_control_store import LocalControlStore
from perfpilot_api.local_device_capture import LocalApkMetadata, LocalDeviceCapture
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import build_ai_projection
from perfpilot_api.services.source_workspaces import SourceBinding
from perfpilot_api.security.agent_signatures import (
    encode_ed25519_public_key,
    encode_signature,
    refresh_proof_message,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_create_local_app = create_local_app


def create_local_app(**kwargs):
    if kwargs.get("control_store") is None:
        data_root = kwargs.get("data_root")
        assert isinstance(data_root, Path)
        control = LocalControlStore(data_root / "control")
        seeded = control.ensure_user("ray_wu", "initial local password", True)
        if seeded.created:
            control.change_password(
                seeded.principal.user_id,
                "initial local password",
                "established local password",
            )
        kwargs["control_store"] = control
    return _create_local_app(**kwargs)


class TestClient(_RawTestClient):
    """Legacy local-runtime tests exercise authenticated browser requests."""

    def __enter__(self) -> "TestClient":
        super().__enter__()
        csrf = self.get("/v1/auth/csrf")
        logged_in = self.post(
            "/v1/auth/login",
            headers={
                "Origin": "http://localhost:3000",
                "x-csrf-token": csrf.json()["csrf_token"],
            },
            json={"username": "ray_wu", "password": "established local password"},
        )
        assert logged_in.status_code == 200, logged_in.text
        return self

    def request(self, method, url, *, headers=None, **kwargs):
        if str(method).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(headers or {})
            headers.setdefault("Origin", "http://localhost:3000")
        return super().request(method, url, headers=headers, **kwargs)


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


def _authenticated_client(client: TestClient, username: str, password: str) -> dict[str, str]:
    csrf = client.get("/v1/auth/csrf")
    assert csrf.status_code == 200
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": "http://localhost:3000", "x-csrf-token": csrf.json()["csrf_token"]},
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    return {"Origin": "http://localhost:3000", "x-csrf-token": login.json()["csrf_token"]}


def test_local_app_persists_team_owned_agents_and_source_workspaces(tmp_path: Path) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    for principal in (first, second):
        control.change_password(
            principal.user_id, "initial user password", "established user password"
        )
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        state_root=state_root,
        control_store=control,
        source_code_analysis_enabled=True,
    )
    private_key = Ed25519PrivateKey.generate()
    workspace_id = UUID("73000000-0000-4000-8000-000000000001")

    with _RawTestClient(app) as first_client, _RawTestClient(app) as second_client:
        first_headers = _authenticated_client(
            first_client, "user01", "established user password"
        )
        _authenticated_client(second_client, "user02", "established user password")
        rejected = first_client.post(
            f"/v1/teams/{first.team_id}/agents/registration-codes",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "/Users/private"},
        )
        assert rejected.status_code == 422
        issued = first_client.post(
            f"/v1/teams/{first.team_id}/agents/registration-codes",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "Build Mac"},
        )
        assert issued.status_code == 201, issued.text
        registered = first_client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1",
                "registration_code": issued.json()["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "build-mac",
                "os_version": "macOS 15",
            },
        )
        assert registered.status_code == 201, registered.text
        credentials = registered.json()
        heartbeat = first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "build-mac",
                "observed_at": datetime.now().astimezone().isoformat(),
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [{
                    "workspace_id": str(workspace_id),
                    "name": "RivotekMedia",
                    "state": "ready",
                    "git_branch": "main",
                    "git_head": "a" * 40,
                    "tracked_dirty_count": 0,
                    "snapshot_policy": "tracked_worktree",
                    "validation_profiles": [],
                }],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert first_client.get(f"/v1/teams/{first.team_id}/agents").json()["agents"][0]["state"] == "online"
        workspaces = first_client.get(f"/v1/teams/{first.team_id}/source-workspaces")
        assert workspaces.status_code == 200
        assert workspaces.json()["workspaces"][0]["workspace_id"] == str(workspace_id)
        renamed = first_client.patch(
            f"/v1/teams/{first.team_id}/agents/{issued.json()['agent_id']}",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "Renamed Mac"},
        )
        assert renamed.status_code == 200
        foreign_binding = first_client.post(
            f"/v1/teams/{first.team_id}/analyses",
            headers=first_headers,
            json={
                "schema_version": "1.1", "analysis_mode": "trace_upload",
                "analysis_profile": "auto", "inputs": [{
                    "kind": "trace", "mime": "application/octet-stream", "size": 1,
                    "sha256_b64": base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                }],
                "source_binding": {
                    "provider_kind": "agent_workspace",
                    "agent_id": "71000000-0000-4000-8000-000000000002",
                    "workspace_id": str(workspace_id),
                    "snapshot_policy": "tracked_worktree",
                    "validation_profile_id": None,
                },
            },
        )
        assert foreign_binding.status_code == 404
        assert second_client.get(f"/v1/teams/{first.team_id}/agents").status_code == 404
        assert second_client.get(f"/v1/teams/{second.team_id}/agents").json()["agents"] == []

    payload = (state_root / "agents" / "agents.json").read_text(encoding="utf-8")
    assert credentials["access_token"] not in payload
    assert credentials["refresh_token"] not in payload
    assert "/Users/" not in payload

    restarted = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        state_root=state_root,
        control_store=control,
        source_code_analysis_enabled=True,
    )
    with _RawTestClient(restarted) as client:
        headers = _authenticated_client(client, "user01", "established user password")
        assert client.get(f"/v1/teams/{first.team_id}/agents", headers=headers).json()["agents"][0]["name"] == "Renamed Mac"
        assert client.get(f"/v1/teams/{first.team_id}/source-workspaces", headers=headers).json()["workspaces"][0]["name"] == "RivotekMedia"


def test_local_agent_control_refresh_unregister_and_team_devices(tmp_path: Path) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    for principal in (first, second):
        control.change_password(principal.user_id, "initial user password", "established user password")
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        control_store=control,
    )
    private_key = Ed25519PrivateKey.generate()
    with _RawTestClient(app) as first_client, _RawTestClient(app) as second_client:
        first_headers = _authenticated_client(first_client, "user01", "established user password")
        _authenticated_client(second_client, "user02", "established user password")
        issued = first_client.post(
            f"/v1/teams/{first.team_id}/agents/registration-codes",
            headers=first_headers,
            json={"schema_version": "1.0", "name": "Build Mac"},
        ).json()
        registered = first_client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.1", "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos", "agent_version": "1.2.3",
                "hostname": "build-mac", "os_version": "macOS 15.0",
            },
        ).json()
        timestamp = int(time.time())
        refreshed = first_client.post(
            "/v1/agent/token/refresh",
            json={
                "schema_version": "1.1", "agent_id": registered["agent_id"],
                "refresh_token": registered["refresh_token"], "nonce": "n" * 22,
                "timestamp": timestamp,
                "signature_b64": encode_signature(private_key.sign(refresh_proof_message(UUID(registered["agent_id"]), "n" * 22, timestamp))),
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        heartbeat_payload = {
            "schema_version": "1.0", "agent_version": "1.2.3", "platform": "macos",
            "hostname": "build-mac", "observed_at": datetime.now().astimezone().isoformat(),
            "clock_skew_ms": 0, "disk_available_bytes": 1024,
            "execution_slot": {"state": "idle", "execution_id": None},
            "devices": [{
                "client_ref": "74000000-0000-4000-8000-000000000001",
                "serial": "emulator-5554", "manufacturer": "Google", "model": "Pixel",
                "android_release": "16", "api_level": 36, "connection_type": "usb",
                "adb_state": "device", "battery_percent": 80, "temperature_c": None,
                "storage_available_bytes": 1024, "property_error_code": None,
            }],
        }
        assert first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {registered['access_token']}"},
            json=heartbeat_payload,
        ).status_code == 401
        assert first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
            json=heartbeat_payload,
        ).status_code == 200
        devices = first_client.get(f"/v1/teams/{first.team_id}/devices")
        assert devices.status_code == 200
        assert devices.json()["devices"][0]["serial_suffix"] == "5554"
        assert second_client.get(f"/v1/teams/{second.team_id}/devices").json()["devices"] == []
        unsupported = first_client.post(
            f"/v1/teams/{first.team_id}/analyses",
            headers=first_headers,
            json={
                "schema_version": "1.0", "analysis_mode": "device",
                "device_id": devices.json()["devices"][0]["device_id"],
                "scenarios": ["cold_start", "scroll", "memory_cycle"],
                "apk": {
                    "artifact_kind": "apk",
                    "mime": "application/vnd.android.package-archive",
                    "size": 1,
                    "sha256_b64": base64.b64encode(hashlib.sha256(b"x").digest()).decode(),
                },
            },
        )
        assert unsupported.status_code == 409
        assert unsupported.json()["error"]["code"] == "remote_device_capture_unavailable"
        revoked = first_client.post(
            "/v1/agent/unregister",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
        )
        assert revoked.status_code == 200
        assert first_client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
            json=heartbeat_payload,
        ).status_code == 401


@pytest.mark.parametrize("value", ["/tmp/agent", r"C:\\agent", r"\\\\server\\agent", "~/agent", "../agent"])
def test_local_agent_registration_rejects_path_shaped_public_metadata(
    tmp_path: Path, value: str
) -> None:
    control = LocalControlStore(tmp_path / "control")
    principal = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(principal.user_id, "initial user password", "established user password")
    state_root = tmp_path / "state"
    app = create_local_app(data_root=tmp_path / "data", state_root=state_root, control_store=control)
    with _RawTestClient(app) as client:
        headers = _authenticated_client(client, "user01", "established user password")
        issued = client.post(
            f"/v1/teams/{principal.team_id}/agents/registration-codes", headers=headers,
            json={"schema_version": "1.0", "name": "Build Mac"},
        ).json()
        rejected = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0", "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(Ed25519PrivateKey.generate().public_key()),
                "platform": "macos", "agent_version": "1.2.3", "hostname": value,
                "os_version": value,
            },
        )
    assert rejected.status_code == 401
    assert value not in (state_root / "agents" / "agents.json").read_text(encoding="utf-8")


def test_local_analyses_are_isolated_between_logged_in_user_teams(
    tmp_path: Path,
) -> None:
    control = LocalControlStore(tmp_path / "control")
    first = control.ensure_user("user01", "initial user password", False).principal
    second = control.ensure_user("user02", "initial user password", False).principal
    control.change_password(
        first.user_id, "initial user password", "established user password"
    )
    control.change_password(
        second.user_id, "initial user password", "established user password"
    )
    data_root = tmp_path / "data"
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=data_root,
        control_store=control,
    )

    with _RawTestClient(app) as first_client, _RawTestClient(app) as second_client:
        first_headers = _authenticated_client(
            first_client, "user01", "established user password"
        )
        second_headers = _authenticated_client(
            second_client, "user02", "established user password"
        )
        analysis_id, checksum = _create_trace_analysis(
            first_client,
            team_id=str(first.team_id),
            headers=first_headers,
        )
        created = first_client.get(f"/v1/teams/{first.team_id}/analyses/{analysis_id}")
        assert created.json()["team_id"] == str(first.team_id)

        first_slot = first_client.post(
            f"/v1/teams/{first.team_id}/analyses/{analysis_id}/uploads",
            headers=first_headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        ).json()["upload"]

        cross_paths = (
            ("GET", f"/v1/teams/{second.team_id}/analyses/{analysis_id}", None),
            (
                "POST",
                f"/v1/teams/{second.team_id}/analyses/{analysis_id}/uploads",
                {
                    "artifact_kind": "trace",
                    "mime": "application/octet-stream",
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            ),
            ("POST", f"/v1/teams/{second.team_id}/analyses/{analysis_id}/cancel", None),
            (
                "POST",
                f"/v1/teams/{second.team_id}/analyses/{analysis_id}/finalize-upload",
                {
                    "upload_id": first_slot["upload_id"],
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            ),
            ("GET", f"/v1/teams/{second.team_id}/analyses/{analysis_id}/report", None),
            (
                "POST",
                f"/v1/teams/{second.team_id}/analyses/{analysis_id}/synthesis-runs",
                None,
            ),
        )
        for method, path, body in cross_paths:
            response = second_client.request(
                method,
                path,
                headers=second_headers,
                json=body,
            )
            assert response.status_code == 404
            assert analysis_id not in response.text
            assert str(first.team_id) not in response.text

        active_first = first_client.get(
            f"/v1/teams/{first.team_id}/analyses?status=active"
        )
        active_second = second_client.get(
            f"/v1/teams/{second.team_id}/analyses?status=active"
        )
        assert [item["analysis_id"] for item in active_first.json()["analyses"]] == [
            analysis_id
        ]
        assert active_second.json()["analyses"] == []

        put = urlsplit(first_slot["put_url"])
        assert (
            first_client.put(
                f"{put.path}?{put.query}",
                content=b"background-local-trace",
                headers=first_slot["required_headers"],
            ).status_code
            == 200
        )
        assert (
            first_client.post(
                f"/v1/teams/{first.team_id}/analyses/{analysis_id}/finalize-upload",
                headers=first_headers,
                json={
                    "upload_id": first_slot["upload_id"],
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            ).status_code
            == 200
        )
        for _ in range(100):
            completed = first_client.get(
                f"/v1/teams/{first.team_id}/analyses/{analysis_id}"
            ).json()
            if completed["report_available"]:
                break
            time.sleep(0.01)
        assert completed["report_available"] is True
        assert (
            first_client.get(
                f"/v1/teams/{first.team_id}/analyses/{analysis_id}/report"
            ).status_code
            == 200
        )
        first_list = first_client.get(f"/v1/teams/{first.team_id}/analyses")
        second_list = second_client.get(f"/v1/teams/{second.team_id}/analyses")
        assert [item["analysis_id"] for item in first_list.json()["analyses"]] == [
            analysis_id
        ]
        assert second_list.json()["analyses"] == []

        first_recovery = first_client.post(
            f"/v1/teams/{first.team_id}/local-recoveries",
            headers=first_headers,
            json=_recovery_request(),
        )
        second_recovery = second_client.post(
            f"/v1/teams/{second.team_id}/local-recoveries",
            headers=second_headers,
            json=_recovery_request(),
        )
        assert first_recovery.status_code == 201
        assert second_recovery.status_code == 201
        assert (
            first_recovery.json()["analysis_id"]
            != second_recovery.json()["analysis_id"]
        )
        assert first_recovery.json()["team_id"] == str(first.team_id)
        assert second_recovery.json()["team_id"] == str(second.team_id)
        for client, owner, recovery in (
            (first_client, first, first_recovery),
            (second_client, second, second_recovery),
        ):
            for _ in range(100):
                recovered_state = client.get(
                    f"/v1/teams/{owner.team_id}/analyses/{recovery.json()['analysis_id']}"
                ).json()
                if recovered_state["report_available"]:
                    break
                time.sleep(0.01)
            assert recovered_state["report_available"] is True

    assert (
        data_root
        / "teams"
        / str(first.team_id)
        / "analyses"
        / analysis_id
        / "state.json"
    ).is_file()
    assert not (data_root / "analyses" / analysis_id).exists()





def test_local_auth_requires_login_and_exposes_current_principal(tmp_path: Path) -> None:
    control = LocalControlStore(tmp_path / "control")
    admin = control.ensure_user("ray_wu", "initial admin password", True).principal
    ordinary = control.ensure_user("user01", "initial user password", False).principal
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        assert client.get("/v1/me").status_code == 401
        csrf = client.get("/v1/auth/csrf")
        assert csrf.status_code == 200
        assert "HttpOnly" in csrf.headers["set-cookie"]
        assert "SameSite=strict" in csrf.headers["set-cookie"]
        assert "Secure" not in csrf.headers["set-cookie"]
        logged_in = client.post(
            "/v1/auth/login",
            headers={"Origin": "http://localhost:3000", "x-csrf-token": csrf.json()["csrf_token"]},
            json={"username": "user01", "password": "initial user password"},
        )
        assert logged_in.status_code == 200
        me = client.get("/v1/me")
        assert me.status_code == 200
        assert me.headers["cache-control"] == "no-store"
        assert me.json()["user"] == {
            "id": str(ordinary.user_id),
            "username": "user01",
            "is_platform_admin": False,
            "must_change_password": True,
        }
        assert me.json()["memberships"] == [
            {"id": str(ordinary.team_id), "team": {"id": str(ordinary.team_id), "name": "user01 local team"}, "role": "owner"}
        ]
        blocked = client.get(f"/v1/teams/{ordinary.team_id}/devices")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "password_change_required"
        assert client.get(f"/v1/teams/{admin.team_id}/devices").status_code == 404


def test_local_auth_changes_initial_password_and_invalidates_old_session(tmp_path: Path) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        headers = _authenticated_client(client, "user01", "initial user password")
        changed = client.post(
            "/v1/auth/change-password",
            headers=headers,
            json={"current_password": "initial user password", "new_password": "changed user password"},
        )
        assert changed.status_code == 200
        assert set(changed.json()) == {"schema_version", "csrf_token"}
        assert client.get("/v1/me").json()["user"]["must_change_password"] is False
        devices = client.get(f"/v1/teams/{user.team_id}/devices")
        assert devices.status_code == 200
        logout = client.post("/v1/auth/logout", headers={"Origin": "http://localhost:3000", "x-csrf-token": changed.json()["csrf_token"]})
        assert logout.status_code == 204
        assert client.get("/v1/me").status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"username": "user01"},
        {"username": "user01", "password": "initial user password", "extra": "no"},
        {"username": 1, "password": "initial user password"},
    ],
)
def test_local_login_rejects_malformed_bodies_as_redacted_invalid_credentials(
    tmp_path: Path,
    body: dict[str, object],
) -> None:
    control = LocalControlStore(tmp_path / "control")
    control.ensure_user("user01", "initial user password", False)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        response = client.post(
            "/v1/auth/login",
            headers={"Origin": "http://localhost:3000", "x-csrf-token": csrf},
            json=body,
        )

    assert response.status_code == 401
    assert set(response.json()) == {"schema_version", "error"}
    assert set(response.json()["error"]) == {"code", "message", "retryable", "request_id"}
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert "user01" not in response.text
    assert "initial user password" not in response.text


def test_local_device_requires_a_changed_authenticated_principal(tmp_path: Path) -> None:
    control = LocalControlStore(tmp_path / "control")
    user = control.ensure_user("user01", "initial user password", False).principal
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        assert client.get("/v1/device").status_code == 401
        headers = _authenticated_client(client, "user01", "initial user password")
        blocked = client.get("/v1/device")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "password_change_required"
        changed = client.post(
            "/v1/auth/change-password",
            headers=headers,
            json={
                "current_password": "initial user password",
                "new_password": "changed user password",
            },
        )
        assert changed.status_code == 200
        assert client.get("/v1/device").status_code == 200
        assert control.resolve_session(client.cookies.get("perfpilot_local_session", "")).username == user.username


def test_concurrent_authenticated_csrf_bootstraps_keep_the_same_session(tmp_path: Path) -> None:
    control = LocalControlStore(tmp_path / "control")
    seeded = control.ensure_user("user01", "initial user password", False).principal
    control.change_password(
        seeded.user_id,
        "initial user password",
        "changed user password",
    )
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        _authenticated_client(client, "user01", "changed user password")
        session_token = client.cookies["perfpilot_local_session"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: client.get("/v1/auth/csrf"), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["csrf_token"] for response in responses}) == 1
    assert all("set-cookie" not in response.headers for response in responses)
    assert control.resolve_session(session_token) is not None


@pytest.mark.parametrize(
    "body",
    [
        {"current_password": "initial user password"},
        {"new_password": "changed user password"},
        {
            "current_password": "initial user password",
            "new_password": "changed user password",
            "marker": "secret-marker",
        },
        {"current_password": 1, "new_password": "changed user password"},
        {"current_password": "x" * 1025, "new_password": "changed user password"},
        {"current_password": "initial user password", "new_password": "x" * 1025},
    ],
)
def test_change_password_validation_is_a_closed_redacted_422(
    tmp_path: Path,
    body: dict[str, object],
) -> None:
    control = LocalControlStore(tmp_path / "control")
    control.ensure_user("user01", "initial user password", False)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path / "data",
        control_store=control,
    )

    with _RawTestClient(app) as client:
        headers = _authenticated_client(client, "user01", "initial user password")
        response = client.post("/v1/auth/change-password", headers=headers, json=body)

    assert response.status_code == 422
    assert set(response.json()) == {"schema_version", "error"}
    assert set(response.json()["error"]) == {"code", "message", "retryable", "request_id"}
    assert response.json()["error"]["code"] == "credential_validation_failed"
    assert "secret-marker" not in response.text
    assert "initial user password" not in response.text


def test_local_runtime_accepts_only_loopback_or_private_lan_http_origins() -> None:
    assert _public_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert _public_origin("http://10.166.0.125:8000") == "http://10.166.0.125:8000"

    with pytest.raises(ValueError, match="loopback or private LAN HTTP"):
        _public_origin("http://8.8.8.8:8000")


def test_local_runtime_rejects_malformed_persisted_ai_round_state() -> None:
    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        _restore_ai_rounds(
            [{"round": 1, "role": "report", "state": [], "attempts": 0}]
        )


def test_local_source_binding_degrades_when_no_source_agent_is_available() -> None:
    document = _source_code_analysis_unavailable_document(
        SourceBinding(
            provider_kind="agent_workspace",
            agent_id=UUID("91000000-0000-4000-8000-000000000001"),
            workspace_id=UUID("92000000-0000-4000-8000-000000000001"),
            snapshot_policy="tracked_worktree",
            validation_profile_id=None,
        )
    )

    assert document["context_state"] == "unavailable"
    assert document["failure_code"] == "source_agent_unavailable"
    assert document["match_summary"] == "none"


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


class _UnavailableAfterRestartSmartPerfettoGateway:
    def __init__(self) -> None:
        self.submissions: list[tuple[bytes, str, str | None]] = []
        self.status_calls = 0
        self.fetch_calls = 0

    async def submit(
        self,
        *,
        trace_path: Path,
        profile: str,
        question: str | None,
    ) -> LocalEngineRun:
        self.submissions.append((trace_path.read_bytes(), profile, question))
        raise AssertionError("AI-only rerun must not submit SmartPerfetto work")

    async def status(self, run: LocalEngineRun) -> str:
        del run
        self.status_calls += 1
        raise AssertionError("AI-only rerun must not query a prior SmartPerfetto session")

    async def fetch_result(self, run: LocalEngineRun) -> EngineResult:
        del run
        self.fetch_calls += 1
        raise AssertionError("AI-only rerun must not fetch a prior SmartPerfetto session")

    async def cancel(self, run: LocalEngineRun) -> None:
        del run
        raise AssertionError("AI-only rerun must not cancel a prior SmartPerfetto session")

    async def aclose(self) -> None:
        return None


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


class _ProjectionReportProvider:
    provider_name = "test-provider"
    model = "test-model"
    prompt_version = "perfpilot-report-v3-test"
    prompt_sha256_b64 = base64.b64encode(hashlib.sha256(b"test-prompt").digest()).decode(
        "ascii"
    )

    async def complete(self, *, projection) -> SynthesisCandidate:
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
                        "user_impact": finding["summary"],
                    }
                )
                if finding["status"] in {"confirmed", "suspected"} and evidence_ids:
                    priority = ("p0", "p1", "p2")[
                        min(len(recommendations), 2)
                    ]
                    recommendations.append(
                        {
                            "priority": priority,
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
            "executive_summary": "单次测试 AI 已完成证据复核。",
            "key_metric_ids": key_metric_ids[:3],
            "top_findings": findings[:3],
            "recommendations": recommendations[:3],
            "source_fixes": [],
            "retest_plan": retest_plan[:3],
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


def _test_synthesizer() -> LocalReportSynthesizer:
    return LocalReportSynthesizer(provider=_ProjectionReportProvider())


class _InvalidReportProvider:
    provider_name = "invalid-test-provider"
    model = "invalid-test-model"
    prompt_version = "perfpilot-report-v3-test"
    prompt_sha256_b64 = base64.b64encode(
        hashlib.sha256(b"invalid-test-prompt").digest()
    ).decode("ascii")

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        del projection
        self.calls += 1
        return SynthesisCandidate(
            candidate_json=b"{}",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
        )

    async def aclose(self) -> None:
        return None


class _RerunBarrierReportProvider(_ProjectionReportProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.rerun_calls = 0
        self.first_rerun_started = threading.Event()
        self.second_rerun_started = threading.Event()
        self.release_reruns = threading.Event()

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        if self.calls > 1:
            self.rerun_calls += 1
            if self.rerun_calls == 1:
                self.first_rerun_started.set()
            else:
                self.second_rerun_started.set()
            while not self.release_reruns.is_set():
                await asyncio.sleep(0.001)
        return await super().complete(projection=projection)


class _AggregateTokenUsageReportProvider(_ProjectionReportProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        if self.calls == 1:
            return SynthesisCandidate(
                candidate_json=b"{}",
                prompt_tokens=3,
                completion_tokens=5,
                latency_ms=7,
            )
        valid = await super().complete(projection=projection)
        return SynthesisCandidate(
            candidate_json=valid.candidate_json,
            prompt_tokens=11,
            completion_tokens=13,
            latency_ms=17,
        )


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
    assert response.json()["team_id"] == team_id
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


def _persist_created_trace_analysis(
    tmp_path: Path,
) -> tuple[str, UUID, dict[str, object]]:
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
    parsed_analysis_id = UUID(analysis_id)
    state = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), parsed_analysis_id)]
    return team_id, parsed_analysis_id, state


def _recovery_request() -> dict[str, object]:
    trace = b"recovered-trace"
    return {
        "schema_version": "1.0",
        "session_id": "session-local-1",
        "run_id": "run-local-1",
        "analysis_profile": "auto",
        "question": "给出最终优化方案",
        "trace_size": len(trace),
        "trace_sha256_b64": base64.b64encode(hashlib.sha256(trace).digest()).decode(
            "ascii"
        ),
    }


def test_local_app_restores_duplicate_upload_ids_without_cross_team_aliasing(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    other_team_id = UUID("82000000-0000-4000-8000-000000000099")
    upload_id = "93000000-0000-4000-8000-000000000001"
    state["inputs"][0]["upload_id"] = upload_id
    state["state"] = "uploading"
    store = LocalAnalysisStore(tmp_path)
    store.save_state(UUID(team_id), analysis_id, state)
    other_state = copy.deepcopy(state)
    other_state["team_id"] = str(other_team_id)
    store.save_state(other_team_id, analysis_id, other_state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with TestClient(app):
        uploads = app.state.local_runtime.uploads
        first_key = (UUID(team_id), analysis_id, upload_id)
        second_key = (other_team_id, analysis_id, upload_id)
        assert set(uploads) == {first_key, second_key}
        assert uploads[first_key].path != uploads[second_key].path
        assert uploads[first_key].token != uploads[second_key].token


def test_local_app_defaults_missing_persisted_ai_rounds_after_restart(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    state.pop("ai_rounds")
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")

    assert restored.status_code == 200
    assert restored.json()["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "pending", "attempts": 0}
    ]


def test_local_source_binding_disabled_persistence_and_old_json_compatibility(
    tmp_path: Path,
) -> None:
    binding = {
        "provider_kind": "agent_workspace",
        "agent_id": "91000000-0000-4000-8000-000000000001",
        "workspace_id": "92000000-0000-4000-8000-000000000001",
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
    }
    body = {
        "schema_version": "1.1",
        "analysis_mode": "trace_upload",
        "analysis_profile": "auto",
        "question": None,
        "inputs": [
            {
                "kind": "trace",
                "mime": "application/octet-stream",
                "size": 4,
                "sha256_b64": base64.b64encode(hashlib.sha256(b"data").digest()).decode(),
            }
        ],
        "source_binding": binding,
    }
    disabled_app = create_local_app(data_root=tmp_path / "disabled")
    with TestClient(disabled_app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        disabled = client.post(
            f"/v1/teams/{team_id}/analyses",
            json=body,
            headers={"x-csrf-token": csrf},
        )
    assert disabled.status_code == 422
    assert disabled.json()["error"]["code"] == "source_code_analysis_disabled"

    enabled_app = create_local_app(
        data_root=tmp_path / "enabled",
        source_code_analysis_enabled=True,
    )
    with TestClient(enabled_app) as client:
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        unavailable = client.post(
            f"/v1/teams/{team_id}/analyses",
            json=body,
            headers={"x-csrf-token": csrf},
        )
    assert unavailable.status_code == 404
    assert unavailable.json()["error"]["code"] == "resource_not_found"


def test_local_app_rejects_present_null_persisted_ai_rounds_after_restart(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    state["ai_rounds"] = None
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


@pytest.mark.parametrize(
    "location",
    [
        "top_level",
        "input",
        "input_descriptor",
        "stages",
        "source_run",
        "source_binding",
        "source_code_analysis",
        "ai_round",
    ],
)
def test_local_app_rejects_unknown_persisted_state_fields_after_restart(
    tmp_path: Path,
    location: str,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    if location == "top_level":
        state["unexpected"] = True
    elif location == "input":
        state["inputs"][0]["unexpected"] = True
    elif location == "input_descriptor":
        state["inputs"][0]["descriptor"]["unexpected"] = True
    elif location == "stages":
        state["stages"]["unexpected"] = "pending"
    elif location == "source_run":
        state["source_run"] = {
            "session_id": "session-local-1",
            "run_id": "run-local-1",
            "unexpected": True,
        }
    elif location == "source_binding":
        state["source_binding"] = {
            "provider_kind": "agent_workspace",
            "agent_id": "91000000-0000-4000-8000-000000000001",
            "workspace_id": "92000000-0000-4000-8000-000000000001",
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": None,
            "unexpected": True,
        }
    elif location == "source_code_analysis":
        state["source_code_analysis"]["unexpected"] = True
    else:
        state["ai_rounds"][0]["unexpected"] = True
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


def test_local_app_rejects_present_null_evidence_format_after_restart(
    tmp_path: Path,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    state["evidence_format_version"] = None
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("mutation", ["marker_only", "manifest_only", "malformed"])
def test_local_app_requires_a_strict_evidence_marker_manifest_pair_after_restart(
    tmp_path: Path,
    mutation: str,
) -> None:
    team_id, analysis_id, state = _persist_created_trace_analysis(tmp_path)
    checksum = base64.b64encode(hashlib.sha256(b"manifest").digest()).decode(
        "ascii"
    )
    manifest = {
        "schema_version": "1.0",
        "normalized_core_sha256_b64": checksum,
        "smartperfetto_report_sha256_b64": checksum,
        "projection_sha256_b64": checksum,
    }
    if mutation != "manifest_only":
        state["evidence_format_version"] = "normalized-core-v1"
    if mutation != "marker_only":
        state["evidence_manifest"] = manifest
    if mutation == "malformed":
        manifest["unexpected"] = True
    LocalAnalysisStore(tmp_path).save_state(UUID(team_id), analysis_id, state)
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="^invalid persisted local analysis$"):
        with TestClient(app):
            pass


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


def test_local_app_does_not_expose_host_adb_through_team_directory(
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
    assert first.json() == {"schema_version": "1.0", "devices": []}
    assert second.json() == {"schema_version": "1.0", "devices": []}
    assert device_probe.calls == 0


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


def test_local_app_rejects_host_device_analysis_without_remote_capture(
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
        device_id = "72000000-0000-4000-8000-000000000001"
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

    assert created.status_code == 409
    assert created.json()["error"]["code"] == "remote_device_capture_unavailable"


@pytest.mark.skip(reason="remote device capture is not wired in the local control plane")
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
    assert workspace.name == "device-captures"
    assert workspace.parent.name == analysis_id
    assert workspace.parents[2].name == team_id
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

    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    restarted_app = create_local_app(
        gateway=restarted_gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(restarted_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
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
            if rerun_state["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert rerun_state["state"] == "partially_completed"
    assert rerun_report.status_code == 200
    assert rerun_report.json()["report_version"] == 2
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0

    store = LocalAnalysisStore(tmp_path)
    parsed_analysis_id = UUID(analysis_id)
    legacy_state = store.load_states()[(UUID(team_id), parsed_analysis_id)]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    store.save_state(UUID(team_id), parsed_analysis_id, legacy_state)
    analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    (analysis_directory / "normalized-core.json").unlink()
    old_report_bytes = (analysis_directory / "report.json").read_bytes()
    old_report = rerun_report.json()
    legacy_provider = _InvalidReportProvider()
    legacy_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    legacy_app = create_local_app(
        gateway=legacy_gateway,
        synthesizer=LocalReportSynthesizer(provider=legacy_provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(legacy_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 3
        for _ in range(100):
            legacy_rerun_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if legacy_rerun_state["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)
        preserved_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert legacy_rerun_state["state"] == "partially_completed"
    assert legacy_rerun_state["failure"]["code"] == "ai_source_evidence_unavailable"
    assert preserved_report.json() == old_report
    assert (analysis_directory / "report.json").read_bytes() == old_report_bytes
    assert legacy_provider.calls == 0
    assert legacy_gateway.submissions == []
    assert legacy_gateway.status_calls == 0
    assert legacy_gateway.fetch_calls == 0
    assert store.load_states()[(UUID(team_id), parsed_analysis_id)]["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "failed", "attempts": 0}
    ]


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


def test_local_app_rolls_back_create_and_reserve_when_persistence_fails(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(app) as client:
        runtime = app.state.local_runtime
        original_save_state = runtime.store.save_state

        def fail_save_state(*_args) -> None:
            raise LocalAnalysisStoreError("local analysis persistence failed")

        runtime.store.save_state = fail_save_state
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        with pytest.raises(LocalAnalysisStoreError):
            _create_trace_analysis(client, team_id=team_id, headers=headers)
        assert runtime.analyses == {}
        assert runtime.uploads == {}

        runtime.store.save_state = original_save_state
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        runtime.store.save_state = fail_save_state
        with pytest.raises(LocalAnalysisStoreError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
                headers=headers,
                json={
                    "artifact_kind": "trace",
                    "mime": "application/octet-stream",
                    "size": len(b"background-local-trace"),
                    "sha256_b64": checksum,
                },
            )
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        assert analysis.inputs["trace"].upload_id is None
        assert runtime.uploads == {}
        runtime.store.save_state = original_save_state
        retry = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        )
        assert retry.status_code == 201


def test_local_app_does_not_launch_finalize_task_before_persistence(
    tmp_path: Path,
) -> None:
    gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(gateway=gateway, data_root=tmp_path)
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        runtime = app.state.local_runtime
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert client.put(
            f"{put.path}?{put.query}",
            content=b"background-local-trace",
            headers=slot["required_headers"],
        ).status_code == 200
        original_save_state = runtime.store.save_state

        def fail_save_state(*_args) -> None:
            raise LocalAnalysisStoreError("local analysis persistence failed")

        runtime.store.save_state = fail_save_state
        with pytest.raises(LocalAnalysisStoreError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
                headers=headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(b"background-local-trace"),
                },
            )
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        assert analysis.inputs["trace"].finalized is False
        assert analysis.task is None
        assert gateway.submissions == []
        runtime.store.save_state = original_save_state
        retry = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
            headers=headers,
            json={
                "upload_id": slot["upload_id"],
                "sha256_b64": checksum,
                "size": len(b"background-local-trace"),
            },
        )
        assert retry.status_code == 200
        for _ in range(100):
            if gateway.submissions:
                break
            time.sleep(0.01)
        assert gateway.submissions


def test_local_app_keeps_committed_finalize_state_when_durability_is_uncertain(
    tmp_path: Path,
) -> None:
    gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    app = create_local_app(gateway=gateway, data_root=tmp_path)
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        analysis_id, checksum = _create_trace_analysis(
            client, team_id=team_id, headers=headers
        )
        runtime = app.state.local_runtime
        slot = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/uploads",
            headers=headers,
            json={
                "artifact_kind": "trace",
                "mime": "application/octet-stream",
                "size": len(b"background-local-trace"),
                "sha256_b64": checksum,
            },
        ).json()["upload"]
        put = urlsplit(slot["put_url"])
        assert client.put(
            f"{put.path}?{put.query}",
            content=b"background-local-trace",
            headers=slot["required_headers"],
        ).status_code == 200
        original_persist = runtime._persist

        async def persist_then_report_uncertain(analysis) -> None:
            await original_persist(analysis)
            raise LocalAnalysisStoreDurabilityError("local analysis durability uncertain")

        runtime._persist = persist_then_report_uncertain
        with pytest.raises(LocalAnalysisStoreDurabilityError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/finalize-upload",
                headers=headers,
                json={
                    "upload_id": slot["upload_id"],
                    "sha256_b64": checksum,
                    "size": len(b"background-local-trace"),
                },
            )
        runtime._persist = original_persist
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        persisted = LocalAnalysisStore(tmp_path).load_states()[
            (UUID(team_id), UUID(analysis_id))
        ]
        assert analysis.inputs["trace"].finalized is True
        assert persisted["inputs"][0]["finalized"] is True
        for _ in range(100):
            if gateway.submissions:
                break
            time.sleep(0.01)
        assert gateway.submissions


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
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
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
        assert (
            validated["synthesis"]["provenance"]["prompt_template_version"]
            == "perfpilot-report-v3-test"
        )
        assert validated["schema_version"] == "1.2"
        assert len(validated["synthesis"]["output"]["key_metric_ids"]) <= 3
        assert len(validated["synthesis"]["output"]["top_findings"]) <= 3
        assert len(validated["synthesis"]["output"]["recommendations"]) <= 3
        assert validated["synthesis"]["output"]["recommendations"]
        analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
        assert (analysis_directory / "round-1.json").is_file()
        assert not (analysis_directory / "round-2.json").exists()
        assert not (analysis_directory / "round-3.json").exists()
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
        UUID(team_id),
        UUID(analysis_id),
        "projection.json",
    )
    assert projection["question"] is None
    assert projection["scenarios"] == []
    assert "/Users/example/private/trace.pb" not in json.dumps(projection)


def test_local_app_persists_bounded_single_pass_failure(tmp_path: Path) -> None:
    provider = _InvalidReportProvider()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=provider),
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
        assert terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "failed", "attempts": 2}
        ]
        published = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert provider.calls == 2
    assert published.status_code == 200
    report = validate_contract("analysis-report", published.json())
    assert report["synthesis"]["state"] == "failed"
    assert report["synthesis"]["failure_code"] == "ai_output_invalid"
    assert report["scenario_reports"][0]["bundle"] is not None
    expected_failed_rounds = [
        {"round": 1, "role": "report", "state": "failed", "attempts": 2}
    ]
    persisted = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))]
    assert persisted["ai_rounds"] == expected_failed_rounds

    restarted_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        data_root=tmp_path,
    )
    with TestClient(restarted_app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")

    assert restored.status_code == 200
    assert restored.json()["ai_rounds"] == expected_failed_rounds


def test_local_app_publishes_aggregate_token_usage_after_a_valid_retry(
    tmp_path: Path,
) -> None:
    provider = _AggregateTokenUsageReportProvider()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=provider),
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
            if terminal["report_available"]:
                break
            time.sleep(0.01)
        assert terminal is not None
        assert terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 2}
        ]
        published = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert provider.calls == 2
    assert published.status_code == 200
    report = validate_contract("analysis-report", published.json())
    provenance = report["synthesis"]["provenance"]
    assert provenance["prompt_tokens"] == 14
    assert provenance["completion_tokens"] == 18
    assert provenance["total_tokens"] == 32


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
    parsed_team_id = UUID(team_id)
    original_state = states[(parsed_team_id, original_id)]
    assert isinstance(original_state.get("created_at"), str)
    assert original_state["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]

    legacy_id = UUID("92000000-0000-4000-8000-000000000004")
    legacy_state = copy.deepcopy(original_state)
    legacy_state["analysis_id"] = str(legacy_id)
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    expected_legacy_rounds = [
        {"round": 1, "role": "extract", "state": "completed", "attempts": 1},
        {"round": 2, "role": "review", "state": "completed", "attempts": 1},
        {"round": 3, "role": "finalize", "state": "completed", "attempts": 1},
    ]
    legacy_state["ai_rounds"] = expected_legacy_rounds
    legacy_report = copy.deepcopy(expected_report)
    legacy_report["analysis_id"] = str(legacy_id)
    assert legacy_report["report_version"] == 1
    store.save_state(parsed_team_id, legacy_id, legacy_state)
    store.save_document(parsed_team_id, legacy_id, "report.json", legacy_report)
    legacy_projection = store.load_document(parsed_team_id, original_id, "projection.json")
    legacy_core = store.load_document(parsed_team_id, original_id, "normalized-core.json")
    legacy_source_report = store.load_document(
        parsed_team_id,
        original_id,
        "smartperfetto-report.json",
    )
    assert legacy_projection is not None
    assert legacy_core is not None
    assert legacy_source_report is not None
    legacy_projection["analysis_id"] = str(legacy_id)
    legacy_core["analysis_id"] = str(legacy_id)
    store.save_document(parsed_team_id, legacy_id, "projection.json", legacy_projection)
    store.save_document(parsed_team_id, legacy_id, "normalized-core.json", legacy_core)
    store.save_document(
        parsed_team_id,
        legacy_id,
        "smartperfetto-report.json",
        legacy_source_report,
    )
    legacy_state["evidence_format_version"] = "normalized-core-v1"
    legacy_state["evidence_manifest"] = _evidence_manifest(
        core=legacy_core,
        source=legacy_source_report,
        projection=legacy_projection,
    )
    store.save_state(parsed_team_id, legacy_id, legacy_state)
    legacy_round_2 = {"sentinel": "legacy-round-2"}
    legacy_round_3 = {"sentinel": "legacy-round-3"}
    store.save_document(parsed_team_id, legacy_id, "round-2.json", legacy_round_2)
    store.save_document(parsed_team_id, legacy_id, "round-3.json", legacy_round_3)
    legacy_directory = tmp_path / "teams" / team_id / "analyses" / str(legacy_id)
    legacy_round_2_bytes = (legacy_directory / "round-2.json").read_bytes()
    legacy_round_3_bytes = (legacy_directory / "round-3.json").read_bytes()

    newer_id = UUID("92000000-0000-4000-8000-000000000002")
    newer_state = copy.deepcopy(original_state)
    newer_state["analysis_id"] = str(newer_id)
    newer_state["created_at"] = "2099-01-01T00:00:00+00:00"
    newer_report = copy.deepcopy(expected_report)
    newer_report["analysis_id"] = str(newer_id)
    store.save_state(parsed_team_id, newer_id, newer_state)
    store.save_document(parsed_team_id, newer_id, "report.json", newer_report)

    pending_id = UUID("92000000-0000-4000-8000-000000000003")
    pending_state = copy.deepcopy(original_state)
    pending_state["analysis_id"] = str(pending_id)
    pending_state["created_at"] = "2100-01-01T00:00:00+00:00"
    pending_state["report_available"] = False
    store.save_state(parsed_team_id, pending_id, pending_state)

    migrated_state = copy.deepcopy(original_state)
    migrated_state.pop("created_at")
    store.save_state(parsed_team_id, original_id, migrated_state)

    restored_gateway = _FakeSmartPerfettoGateway(_smartperfetto_result())
    second_app = create_local_app(
        gateway=restored_gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        restored = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}")
        assert restored.status_code == 200
        assert restored.json()["report_available"] is True
        assert restored.json()["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
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
        legacy = client.get(f"/v1/teams/{team_id}/analyses/{legacy_id}")
        assert legacy.status_code == 200
        assert legacy.json()["ai_rounds"] == expected_legacy_rounds
        assert store.load_document(parsed_team_id, legacy_id, "round-2.json") == legacy_round_2
        assert store.load_document(parsed_team_id, legacy_id, "round-3.json") == legacy_round_3
        assert (legacy_directory / "round-2.json").read_bytes() == legacy_round_2_bytes
        assert (legacy_directory / "round-3.json").read_bytes() == legacy_round_3_bytes

        legacy_version = legacy.json()["version"]
        second_headers = {
            "x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]
        }
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{legacy_id}/synthesis-runs",
            headers=second_headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 2
        legacy_terminal = legacy.json()
        for _ in range(100):
            legacy_terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{legacy_id}"
            ).json()
            if (
                legacy_terminal["version"] > legacy_version
                and legacy_terminal["stages"][2]["state"] == "completed"
                and legacy_terminal["stages"][3]["state"] == "completed"
            ):
                break
            time.sleep(0.01)
        assert legacy_terminal["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{legacy_id}/report"
        )
        assert rerun_report.status_code == 200
        assert rerun_report.json()["report_version"] == 2
        assert store.load_document(parsed_team_id, legacy_id, "round-1.json") is not None
        assert store.load_document(parsed_team_id, legacy_id, "round-2.json") == legacy_round_2
        assert store.load_document(parsed_team_id, legacy_id, "round-3.json") == legacy_round_3
        assert (legacy_directory / "round-2.json").read_bytes() == legacy_round_2_bytes
        assert (legacy_directory / "round-3.json").read_bytes() == legacy_round_3_bytes

    assert restored_gateway.submissions == []


def test_local_app_reruns_ai_from_persisted_evidence_after_restart(
    tmp_path: Path,
) -> None:
    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
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
        for _ in range(100):
            first_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if first_state["report_available"]:
                break
            time.sleep(0.01)
        assert first_state["state"] == "completed"

    legacy_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    (legacy_directory / "normalized-core.json").unlink(missing_ok=True)
    legacy_store = LocalAnalysisStore(tmp_path)
    legacy_state = legacy_store.load_states()[(UUID(team_id), UUID(analysis_id))]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    legacy_store.save_state(UUID(team_id), UUID(analysis_id), legacy_state)
    assert {
        "projection.json",
        "report.json",
        "smartperfetto-report.json",
    }.issubset(path.name for path in legacy_directory.iterdir())

    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    second_app = create_local_app(
        gateway=restarted_gateway,
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
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
            if rerun_state["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert rerun_state["state"] == "completed"
    assert rerun_state["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]
    assert rerun_report.status_code == 200
    assert rerun_report.json()["report_version"] == 2
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0
    migrated_core = LocalAnalysisStore(tmp_path).load_document(
        UUID(team_id),
        UUID(analysis_id),
        "normalized-core.json",
    )
    assert migrated_core is not None
    assert validate_contract("normalized-trace-report", migrated_core)[
        "analysis_id"
    ] == analysis_id
    assert LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))][
        "evidence_format_version"
    ] == "normalized-core-v1"

    store = LocalAnalysisStore(tmp_path)
    parsed_analysis_id = UUID(analysis_id)
    parsed_team_id = UUID(team_id)
    legacy_state = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    store.save_state(parsed_team_id, parsed_analysis_id, legacy_state)
    (legacy_directory / "normalized-core.json").unlink()
    tampered_source = store.load_document(
        parsed_team_id,
        parsed_analysis_id,
        "smartperfetto-report.json",
    )
    assert tampered_source is not None
    startup_envelope = tampered_source["dataEnvelopes"][0]
    startup_envelope["evidence"][0]["fields"]["duration_ms"] = 123.4
    startup_envelope["columns"][0]["value"] = 123.4
    analysis = second_app.state.local_runtime.analyses[(parsed_team_id, parsed_analysis_id)]
    tampered = _prepare_local_report(
        analysis,
        EngineResult(
            contract="workspace-agent-v1",
            state="completed",
            payload={
                "reportId": tampered_source["reportId"],
                "report": tampered_source,
            },
        ),
    )
    store.save_document(
        parsed_team_id,
        parsed_analysis_id,
        "smartperfetto-report.json",
        tampered.source_report,
    )
    store.save_document(
        parsed_team_id,
        parsed_analysis_id,
        "projection.json",
        tampered.projection.document,
    )
    old_report_bytes = (legacy_directory / "report.json").read_bytes()
    old_report = rerun_report.json()
    tamper_provider = _InvalidReportProvider()
    tamper_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    third_app = create_local_app(
        gateway=tamper_gateway,
        synthesizer=LocalReportSynthesizer(provider=tamper_provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(third_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        assert rerun.json()["generation"] == 3
        for _ in range(100):
            tampered_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if tampered_state["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)
        preserved = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert tampered_state["failure"] == {
        "code": "ai_source_evidence_invalid",
        "message": "PerfPilot AI 最终报告无法从已保存的分析证据生成",
        "retryable": False,
    }
    assert preserved.json() == old_report
    assert (legacy_directory / "report.json").read_bytes() == old_report_bytes
    assert tamper_provider.calls == 0
    assert tamper_gateway.submissions == []
    assert tamper_gateway.status_calls == 0
    assert tamper_gateway.fetch_calls == 0


@pytest.mark.parametrize(
    ("core_mutation", "expected_failure_code"),
    [
        ("missing", "ai_source_evidence_unavailable"),
        ("corrupt", "ai_source_evidence_invalid"),
        ("consistent_tamper", "ai_source_evidence_invalid"),
    ],
)
def test_local_app_rejects_invalid_versioned_core_and_keeps_the_old_report(
    tmp_path: Path,
    core_mutation: str,
    expected_failure_code: str,
) -> None:
    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
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
        for _ in range(100):
            first_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if first_state["report_available"]:
                break
            time.sleep(0.01)
        old_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    store = LocalAnalysisStore(tmp_path)
    parsed_team_id = UUID(team_id)
    parsed_analysis_id = UUID(analysis_id)
    state = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    assert state["evidence_format_version"] == "normalized-core-v1"
    original_core = store.load_document(parsed_team_id, parsed_analysis_id, "normalized-core.json")
    assert original_core is not None
    analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    old_report_bytes = (analysis_directory / "report.json").read_bytes()
    if core_mutation == "missing":
        (analysis_directory / "normalized-core.json").unlink()
    elif core_mutation == "corrupt":
        store.save_document(
            parsed_team_id,
            parsed_analysis_id,
            "normalized-core.json",
            {"schema_version": "invalid"},
        )
    else:
        scenario = original_core["scenario_reports"][0]
        metric = scenario["metrics"][0]
        metric["numeric_value"] = float(metric["numeric_value"]) + 1.0
        core_bytes = canonical_json_bytes(original_core)
        tampered_projection = build_ai_projection(
            NormalizedTraceReport(
                canonical_bytes=core_bytes,
                sha256_b64=base64.b64encode(
                    hashlib.sha256(core_bytes).digest()
                ).decode("ascii"),
            ),
            analysis_profile=state["profile"],
            question=state["question"],
        )
        store.save_document(
            parsed_team_id,
            parsed_analysis_id,
            "normalized-core.json",
            original_core,
        )
        store.save_document(
            parsed_team_id,
            parsed_analysis_id,
            "projection.json",
            tampered_projection.document,
        )
    provider = _InvalidReportProvider()
    restarted_gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    second_app = create_local_app(
        gateway=restarted_gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        for _ in range(100):
            failed_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if failed_state["state"] in {"completed", "partially_completed", "failed"}:
                break
            time.sleep(0.01)
        preserved_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert failed_state["state"] == "partially_completed"
    assert failed_state["failure"] == {
        "code": expected_failure_code,
        "message": "PerfPilot AI 最终报告无法从已保存的分析证据生成",
        "retryable": False,
    }
    assert failed_state["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "failed", "attempts": 0}
    ]
    assert [item["state"] for item in failed_state["stages"]] == [
        "completed",
        "completed",
        "failed",
        "completed",
    ]
    assert preserved_report.status_code == 200
    assert preserved_report.json() == old_report
    assert (analysis_directory / "report.json").read_bytes() == old_report_bytes
    assert provider.calls == 0
    assert restarted_gateway.submissions == []
    assert restarted_gateway.status_calls == 0
    assert restarted_gateway.fetch_calls == 0


@pytest.mark.parametrize(
    "failed_document",
    ["smartperfetto-report.json", "projection.json"],
)
def test_local_app_does_not_publish_a_manifest_for_partial_evidence_writes(
    tmp_path: Path,
    failed_document: str,
) -> None:
    first_app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(first_app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=_recovery_request(),
        )
        assert recovered.status_code == 201
        analysis_id = recovered.json()["analysis_id"]
        for _ in range(100):
            initial_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if initial_state["report_available"]:
                break
            time.sleep(0.01)
        old_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        ).json()

    store = LocalAnalysisStore(tmp_path)
    parsed_team_id = UUID(team_id)
    parsed_analysis_id = UUID(analysis_id)
    legacy_state = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    legacy_state.pop("evidence_format_version", None)
    legacy_state.pop("evidence_manifest", None)
    store.save_state(parsed_team_id, parsed_analysis_id, legacy_state)
    analysis_directory = tmp_path / "teams" / team_id / "analyses" / analysis_id
    (analysis_directory / "normalized-core.json").unlink()
    old_report_bytes = (analysis_directory / "report.json").read_bytes()
    provider = _InvalidReportProvider()
    gateway = _UnavailableAfterRestartSmartPerfettoGateway()
    second_app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )
    with TestClient(second_app) as client:
        runtime = second_app.state.local_runtime
        original_save_document = runtime.store.save_document

        def fail_selected_document(
            stored_team_id: UUID,
            stored_analysis_id: UUID,
            name: str,
            value: dict[str, object],
        ) -> None:
            if name == failed_document:
                raise RuntimeError("evidence document write failed")
            original_save_document(stored_team_id, stored_analysis_id, name, value)

        runtime.store.save_document = fail_selected_document
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        rerun = client.post(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
            headers=headers,
        )
        assert rerun.status_code == 201
        for _ in range(100):
            failed_state = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if failed_state["state"] in {
                "completed",
                "partially_completed",
                "failed",
            }:
                break
            time.sleep(0.01)
        preserved_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    persisted = store.load_states()[(parsed_team_id, parsed_analysis_id)]
    assert "evidence_format_version" not in persisted
    assert "evidence_manifest" not in persisted
    assert preserved_report.json() == old_report
    assert (analysis_directory / "report.json").read_bytes() == old_report_bytes
    assert provider.calls == 0
    assert gateway.submissions == []
    assert gateway.status_calls == 0
    assert gateway.fetch_calls == 0


def test_local_app_installs_recovery_task_before_persisting_initial_state(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    provider.release_reruns.set()
    gateway = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        runtime = app.state.local_runtime
        original_persist = runtime._persist
        recovery_persist_entered = threading.Event()
        release_recovery_persist = threading.Event()
        blocked_recovery = False

        async def persist_with_recovery_barrier(analysis) -> None:
            nonlocal blocked_recovery
            if (
                not blocked_recovery
                and analysis.source_run is not None
                and analysis.generation == 1
                and analysis.report is None
            ):
                blocked_recovery = True
                recovery_persist_entered.set()
                while not release_recovery_persist.is_set():
                    await asyncio.sleep(0.001)
            await original_persist(analysis)

        runtime._persist = persist_with_recovery_barrier
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovery_path = f"/v1/teams/{team_id}/local-recoveries"
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                client.post,
                recovery_path,
                headers=headers,
                json=_recovery_request(),
            )
            entered = recovery_persist_entered.wait(timeout=2)
            if not entered:
                release_recovery_persist.set()
                first_future.result(timeout=5)
            assert entered
            assert provider.calls == 0
            try:
                duplicate = client.post(
                    recovery_path,
                    headers=headers,
                    json=_recovery_request(),
                )
                assert duplicate.status_code == 201
                analysis_id = duplicate.json()["analysis_id"]
                rerun = client.post(
                    f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
                    headers=headers,
                )
            finally:
                release_recovery_persist.set()
            first = first_future.result(timeout=5)

        assert first.status_code == 201
        assert first.json()["analysis_id"] == analysis_id
        assert rerun.status_code == 409
        assert rerun.json() == {"detail": "analysis is already running"}
        terminal = first.json()
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if terminal["report_available"]:
                break
            time.sleep(0.01)
        report = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}/report")

    assert provider.calls == 1
    assert report.status_code == 200
    assert report.json()["report_version"] == 1
    persisted = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))]
    assert persisted["generation"] == 1
    assert gateway.submissions == []


def test_local_app_removes_recovery_when_initial_persistence_fails(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_live_smartperfetto_result()),
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
    )

    with TestClient(app) as client:
        runtime = app.state.local_runtime

        async def fail_recovery_persist(_analysis) -> None:
            raise RuntimeError("recovery persistence failed")

        runtime._persist = fail_recovery_persist
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        with pytest.raises(RuntimeError, match="^recovery persistence failed$"):
            client.post(
                f"/v1/teams/{team_id}/local-recoveries",
                headers=headers,
                json=_recovery_request(),
            )

        assert runtime.analyses == {}
        assert runtime.tasks == set()
        assert provider.calls == 0

    assert LocalAnalysisStore(tmp_path).load_states() == {}


def test_local_app_retains_committed_recovery_after_durability_error(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        poll_interval_seconds=0.001,
    )
    with TestClient(app) as client:
        runtime = app.state.local_runtime
        original_persist = runtime._persist
        injected = False

        async def persist_then_report_uncertain(analysis) -> None:
            nonlocal injected
            await original_persist(analysis)
            if not injected:
                injected = True
                raise LocalAnalysisStoreDurabilityError(
                    "local analysis durability uncertain"
                )

        runtime._persist = persist_then_report_uncertain
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        with pytest.raises(LocalAnalysisStoreDurabilityError):
            client.post(
                f"/v1/teams/{team_id}/local-recoveries",
                headers=headers,
                json=_recovery_request(),
            )
        runtime._persist = original_persist
        assert len(runtime.analyses) == 1
        (_, analysis_id), analysis = next(iter(runtime.analyses.items()))
        persisted = LocalAnalysisStore(tmp_path).load_states()[
            (UUID(team_id), analysis_id)
        ]
        assert analysis.generation == persisted["generation"] == 1
        assert analysis.task is not None


def test_local_app_retains_committed_rerun_after_durability_error(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        gateway=_FakeSmartPerfettoGateway(_smartperfetto_result()),
        synthesizer=_test_synthesizer(),
        data_root=tmp_path,
        poll_interval_seconds=0.001,
    )
    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=_recovery_request(),
        )
        analysis_id = recovered.json()["analysis_id"]
        for _ in range(100):
            current = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if current["report_available"]:
                break
            time.sleep(0.01)
        runtime = app.state.local_runtime
        original_persist = runtime._persist
        injected = False

        async def persist_then_report_uncertain(analysis) -> None:
            nonlocal injected
            await original_persist(analysis)
            if not injected and analysis.generation == 2:
                injected = True
                raise LocalAnalysisStoreDurabilityError(
                    "local analysis durability uncertain"
                )

        runtime._persist = persist_then_report_uncertain
        with pytest.raises(LocalAnalysisStoreDurabilityError):
            client.post(
                f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs",
                headers=headers,
            )
        runtime._persist = original_persist
        analysis = runtime.analyses[(UUID(team_id), UUID(analysis_id))]
        persisted = LocalAnalysisStore(tmp_path).load_states()[
            (UUID(team_id), UUID(analysis_id))
        ]
        assert analysis.generation == persisted["generation"] == 2
        assert analysis.task is not None


def test_local_app_serializes_simultaneous_ai_rerun_reservations(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    gateway = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
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
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json={
                "schema_version": "1.0",
                "session_id": "session-local-1",
                "run_id": "run-local-1",
                "analysis_profile": "auto",
                "question": "给出最终优化方案",
                "trace_size": len(b"recovered-trace"),
                "trace_sha256_b64": checksum,
            },
        )
        assert recovered.status_code == 201
        analysis_id = recovered.json()["analysis_id"]
        initial = recovered.json()
        for _ in range(100):
            initial = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if initial["report_available"] and initial["state"] in {
                "completed",
                "partially_completed",
            }:
                break
            time.sleep(0.01)
        runtime = app.state.local_runtime
        parsed_analysis_id = UUID(analysis_id)
        for _ in range(100):
            initial_task = runtime.analyses[(UUID(team_id), parsed_analysis_id)].task
            if initial_task is not None and initial_task.done():
                break
            time.sleep(0.01)
        assert initial["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        assert provider.calls == 1
        initial_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )
        assert initial_report.status_code == 200
        assert initial_report.json()["report_version"] == 1

        original_persist = runtime._persist
        first_reservation_entered = threading.Event()
        second_reservation_entered = threading.Event()
        release_reservations = threading.Event()
        reservation_persists = 0

        async def persist_with_reservation_barrier(analysis) -> None:
            nonlocal reservation_persists
            is_reservation = (
                analysis.analysis_id == parsed_analysis_id
                and analysis.generation >= 2
                and len(analysis.ai_rounds) == 1
                and analysis.ai_rounds[0].state == "pending"
                and analysis.stages["report"] == "pending"
            )
            if is_reservation:
                reservation_persists += 1
                if reservation_persists == 1:
                    first_reservation_entered.set()
                else:
                    second_reservation_entered.set()
                while not release_reservations.is_set():
                    await asyncio.sleep(0.001)
            await original_persist(analysis)

        runtime._persist = persist_with_reservation_barrier
        rerun_path = f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs"
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                client.post,
                rerun_path,
                headers=headers,
            )
            first_entered = first_reservation_entered.wait(timeout=2)
            if not first_entered:
                release_reservations.set()
                first_future.result(timeout=5)
            assert first_entered
            assert not provider.first_rerun_started.is_set()
            second_future = executor.submit(
                client.post,
                rerun_path,
                headers=headers,
            )
            deadline = time.monotonic() + 2
            while (
                not second_future.done()
                and not second_reservation_entered.is_set()
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            second_observed = (
                second_future.done() or second_reservation_entered.is_set()
            )
            release_reservations.set()
            first_rerun = first_future.result(timeout=5)
            second_rerun = second_future.result(timeout=5)
        assert second_observed

        responses = (first_rerun, second_rerun)
        try:
            assert sorted(response.status_code for response in responses) == [201, 409]
            accepted = next(response for response in responses if response.status_code == 201)
            rejected = next(response for response in responses if response.status_code == 409)
            assert accepted.json()["generation"] == 2
            assert rejected.json() == {"detail": "analysis is already running"}
            assert provider.first_rerun_started.wait(timeout=2)
            assert provider.rerun_calls == 1
            assert not provider.second_rerun_started.is_set()
        finally:
            provider.release_reruns.set()

        terminal = initial
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if (
                terminal["stages"][2]["state"] == "completed"
                and terminal["stages"][3]["state"] == "completed"
                and terminal["ai_rounds"]
                == [
                    {
                        "round": 1,
                        "role": "report",
                        "state": "completed",
                        "attempts": 1,
                    }
                ]
            ):
                break
            time.sleep(0.01)
        final_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    assert terminal["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]
    assert provider.calls == 2
    assert provider.rerun_calls == 1
    assert final_report.status_code == 200
    assert final_report.json()["report_version"] == 2
    persisted = LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), UUID(analysis_id))]
    assert persisted["generation"] == 2
    assert persisted["ai_rounds"] == terminal["ai_rounds"]
    assert gateway.submissions == []


def test_local_app_rolls_back_a_failed_rerun_persistence_reservation(
    tmp_path: Path,
) -> None:
    provider = _RerunBarrierReportProvider()
    gateway = _FakeSmartPerfettoGateway(_live_smartperfetto_result())
    app = create_local_app(
        gateway=gateway,
        synthesizer=LocalReportSynthesizer(provider=provider),
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0.001,
    )

    with TestClient(app) as client:
        headers = {"x-csrf-token": client.get("/v1/auth/csrf").json()["csrf_token"]}
        team_id = client.get("/v1/me").json()["memberships"][0]["team"]["id"]
        recovered = client.post(
            f"/v1/teams/{team_id}/local-recoveries",
            headers=headers,
            json=_recovery_request(),
        )
        assert recovered.status_code == 201
        analysis_id = recovered.json()["analysis_id"]
        parsed_analysis_id = UUID(analysis_id)
        before = recovered.json()
        for _ in range(100):
            before = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if before["report_available"]:
                break
            time.sleep(0.01)
        runtime = app.state.local_runtime
        for _ in range(100):
            initial_task = runtime.analyses[(UUID(team_id), parsed_analysis_id)].task
            if initial_task is not None and initial_task.done():
                break
            time.sleep(0.01)
        persisted_before = copy.deepcopy(
            LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), parsed_analysis_id)]
        )
        original_persist = runtime._persist
        fail_reservation = True

        async def fail_first_rerun_persist(analysis) -> None:
            nonlocal fail_reservation
            if (
                fail_reservation
                and analysis.analysis_id == parsed_analysis_id
                and analysis.generation == 2
                and analysis.ai_rounds[0].state == "pending"
            ):
                fail_reservation = False
                raise RuntimeError("rerun persistence failed")
            await original_persist(analysis)

        runtime._persist = fail_first_rerun_persist
        rerun_path = f"/v1/teams/{team_id}/analyses/{analysis_id}/synthesis-runs"
        with pytest.raises(RuntimeError, match="^rerun persistence failed$"):
            client.post(rerun_path, headers=headers)

        current = runtime.analyses[(UUID(team_id), parsed_analysis_id)]
        assert current.task is None
        assert current.generation == persisted_before["generation"]
        assert current.state == persisted_before["state"]
        assert current.stages == persisted_before["stages"]
        assert current.version == persisted_before["version"]
        assert provider.calls == 1
        assert provider.rerun_calls == 0
        assert not provider.first_rerun_started.is_set()
        restored = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}"
        ).json()
        assert restored["state"] == before["state"]
        assert restored["version"] == before["version"]
        assert restored["stages"] == before["stages"]
        assert restored["ai_rounds"] == before["ai_rounds"]
        assert LocalAnalysisStore(tmp_path).load_states()[(UUID(team_id), parsed_analysis_id)] == (
            persisted_before
        )

        successful = client.post(rerun_path, headers=headers)
        assert successful.status_code == 201
        assert successful.json()["generation"] == 2
        try:
            assert provider.first_rerun_started.wait(timeout=2)
            assert provider.rerun_calls == 1
        finally:
            provider.release_reruns.set()
        terminal = restored
        for _ in range(100):
            terminal = client.get(
                f"/v1/teams/{team_id}/analyses/{analysis_id}"
            ).json()
            if (
                terminal["stages"][2]["state"] == "completed"
                and terminal["stages"][3]["state"] == "completed"
            ):
                break
            time.sleep(0.01)
        report = client.get(f"/v1/teams/{team_id}/analyses/{analysis_id}/report")

    assert terminal["ai_rounds"] == [
        {"round": 1, "role": "report", "state": "completed", "attempts": 1}
    ]
    assert provider.calls == 2
    assert report.status_code == 200
    assert report.json()["report_version"] == 2
    assert gateway.submissions == []


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
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
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
        assert rerun_state["ai_rounds"] == [
            {"round": 1, "role": "report", "state": "completed", "attempts": 1}
        ]
        rerun_report = client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )
        assert rerun_report.status_code == 200
        assert rerun_report.json()["report_version"] == 2
        assert gateway.submissions == []
