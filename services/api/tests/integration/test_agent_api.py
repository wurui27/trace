from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import perfpilot_api.main as main_module
from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    InMemoryAgentNonceStore,
    encode_ed25519_public_key,
    encode_signature,
    refresh_proof_message,
)
from perfpilot_api.security.proxy_signature import InMemoryReplayStore, sign_proxy_request
from perfpilot_api.services.agents import (
    AgentService,
    InMemoryAgentRepository,
    TaskSigningKey,
)
from perfpilot_api.services.device_directory import (
    DeviceDirectory,
    InMemoryDeviceDirectoryRepository,
)
from perfpilot_api.services.auth import TeamRequestContext

TEAM_A_ID = UUID("20000000-0000-4000-8000-000000000001")
TEAM_B_ID = UUID("20000000-0000-4000-8000-000000000002")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
PROXY_SECRET = b"test-proxy-secret"
ORIGIN = "https://app.example"


class FakeAuthService:
    def __init__(self, *, role: str = "team_owner") -> None:
        self.role = role

    async def authorize_team_request(
        self,
        *,
        session_token: str,
        csrf_token: str,
        team_id: UUID,
        access: str,
    ) -> TeamRequestContext:
        assert session_token == "session-token"
        assert csrf_token == "csrf-token"
        assert access in {"read", "write"}
        return TeamRequestContext(user_id=USER_ID, team_id=team_id, role=self.role)


class CountingEntropy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, size: int) -> bytes:
        self.calls += 1
        return self.calls.to_bytes(4, "big") * (size // 4)


def _services() -> tuple[AgentService, DeviceDirectory]:
    task_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    repository = InMemoryAgentRepository(uuid_factory=uuid4)
    agent_service = AgentService(
        repository=repository,
        credentials=AgentCredentialCodec(
            b"agent-api-credential-secret-12345",
            entropy=CountingEntropy(),
        ),
        nonce_store=InMemoryAgentNonceStore(
            key_secret=b"n" * 32,
            clock=lambda: NOW.timestamp(),
        ),
        task_signing_key=TaskSigningKey(
            kid="lan-api-test",
            public_key_b64=encode_ed25519_public_key(task_key.public_key()),
        ),
        clock=lambda: NOW,
    )
    return (
        agent_service,
        DeviceDirectory(
            repository=InMemoryDeviceDirectoryRepository(repository),
            serial_hmac_key=b"s" * 32,
            clock=lambda: NOW,
        ),
    )


def _settings(*, source_code_analysis_enabled: bool = False) -> Settings:
    return Settings(
        app_env="test",
        proxy_secret=PROXY_SECRET.decode(),
        allowed_origins=[ORIGIN],
        source_code_analysis_enabled=source_code_analysis_enabled,
        _env_file=None,
        _secrets_dir=None,
    )


def _proxy_headers(*, method: str, target: str, body: bytes, request_id: str) -> dict[str, str]:
    signature = sign_proxy_request(
        PROXY_SECRET,
        timestamp=int(NOW.timestamp()),
        request_id=request_id,
        method=method,
        raw_path=target.encode("ascii"),
        raw_query=b"",
        body=body,
    )
    return {
        "x-perfpilot-proxy-timestamp": str(int(NOW.timestamp())),
        "x-perfpilot-proxy-signature": signature,
        "x-request-id": request_id,
        "origin": ORIGIN,
        "x-csrf-token": "csrf-token",
        "cookie": "perfpilot_session=session-token",
        "content-type": "application/json",
    }


def _client(
    *,
    role: str = "team_owner",
    source_code_analysis_enabled: bool = False,
) -> TestClient:
    agent_service, device_directory = _services()
    return TestClient(
        create_app(
            testing=True,
            settings_override=_settings(
                source_code_analysis_enabled=source_code_analysis_enabled
            ),
            auth_service=FakeAuthService(role=role),  # type: ignore[arg-type]
            agent_service=agent_service,
            device_directory=device_directory,
            replay_store=InMemoryReplayStore(clock=lambda: NOW.timestamp()),
            proxy_clock=lambda: NOW.timestamp(),
        )
    )


def _browser_request(
    client: TestClient,
    *,
    method: str,
    target: str,
    payload: dict[str, object],
    request_id: str,
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.request(
        method,
        target,
        content=body,
        headers=_proxy_headers(
            method=method,
            target=target,
            body=body,
            request_id=request_id,
        ),
    )


def _create_code(client: TestClient, *, name: str = "Ray Mac") -> dict[str, object]:
    target = f"/v1/teams/{TEAM_A_ID}/agents/registration-codes"
    response = _browser_request(
        client,
        method="POST",
        target=target,
        payload={"schema_version": "1.0", "name": name},
        request_id="req-create-code",
    )
    assert response.status_code == 201
    return response.json()


def test_agent_registers_without_browser_proxy_headers_and_response_is_no_store() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    with _client() as client:
        issued = _create_code(client)
        response = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "Ray Mac",
                "os_version": "macOS 15.6",
            },
            headers={"x-request-id": "req-register"},
        )

    assert response.status_code == 201
    assert response.json()["agent_id"] == issued["agent_id"]
    assert response.json()["access_token"].startswith("ppat_")
    assert response.json()["refresh_token"].startswith("pprt_")
    assert response.headers["cache-control"] == "no-store"


def test_agent_can_revoke_its_own_credentials() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    with _client() as client:
        issued = _create_code(client)
        registered = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "linux",
                "agent_version": "1.2.3",
                "hostname": "Ubuntu Agent",
                "os_version": "Ubuntu 24.04",
            },
        ).json()
        headers = {"authorization": f"Bearer {registered['access_token']}"}
        revoked = client.post("/v1/agent/unregister", headers=headers)
        replay = client.post("/v1/agent/unregister", headers=headers)

    assert revoked.status_code == 200
    assert revoked.json() == {
        "schema_version": "1.0",
        "agent_id": registered["agent_id"],
        "state": "revoked",
    }
    assert revoked.headers["cache-control"] == "no-store"
    assert replay.status_code == 401


def test_browser_agent_routes_reject_missing_proxy_headers() -> None:
    with _client() as client:
        response = client.get(f"/v1/teams/{TEAM_A_ID}/agents")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proxy_authentication_failed"


def test_agent_list_is_redacted_and_cross_team_mutation_returns_404() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key_b64 = encode_ed25519_public_key(private_key.public_key())
    with _client() as client:
        issued = _create_code(client)
        registered = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": public_key_b64,
                "platform": "linux",
                "agent_version": "1.2.3",
                "hostname": "Ubuntu Agent",
                "os_version": "Ubuntu 24.04",
            },
        ).json()
        list_target = f"/v1/teams/{TEAM_A_ID}/agents"
        listed = _browser_request(
            client,
            method="GET",
            target=list_target,
            payload={},
            request_id="req-list-agents",
        )
        patch_target = f"/v1/teams/{TEAM_B_ID}/agents/{issued['agent_id']}"
        patched = _browser_request(
            client,
            method="PATCH",
            target=patch_target,
            payload={"schema_version": "1.0", "name": "Other"},
            request_id="req-cross-team",
        )

    assert listed.status_code == 200
    assert listed.json()["agents"][0]["name"] == "Ray Mac"
    redacted_text = listed.text + patched.text
    assert issued["registration_code"] not in redacted_text
    assert registered["access_token"] not in redacted_text
    assert registered["refresh_token"] not in redacted_text
    assert public_key_b64 not in redacted_text
    assert patched.status_code == 404
    assert patched.json()["error"]["code"] == "resource_not_found"


def test_non_owner_cannot_create_registration_code() -> None:
    with _client(role="team_member") as client:
        target = f"/v1/teams/{TEAM_A_ID}/agents/registration-codes"
        response = _browser_request(
            client,
            method="POST",
            target=target,
            payload={"schema_version": "1.0", "name": "Ray Mac"},
            request_id="req-member-create",
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_forbidden"


def test_agent_request_models_reject_unknown_fields_without_echoing_credentials() -> None:
    secret_marker = "ppreg_" + "A" * 43
    with _client() as client:
        response = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": secret_marker,
                "public_key_b64": "A" * 43 + "=",
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "Ray Mac",
                "os_version": "macOS 15.6",
                "team_id": str(TEAM_A_ID),
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert secret_marker not in response.text


def test_refresh_endpoint_rotates_tokens_and_old_refresh_fails() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    with _client() as client:
        issued = _create_code(client)
        registered = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "windows",
                "agent_version": "1.2.3",
                "hostname": "Windows Agent",
                "os_version": "Windows 11",
            },
        ).json()
        nonce = "cmVmcmVzaC1hcGktbm9uY2UtMDAwMDAwMDA"
        timestamp = int(NOW.timestamp())
        refreshed = client.post(
            "/v1/agent/token/refresh",
            json={
                "schema_version": "1.0",
                "agent_id": registered["agent_id"],
                "refresh_token": registered["refresh_token"],
                "nonce": nonce,
                "timestamp": timestamp,
                "signature_b64": encode_signature(
                    private_key.sign(
                        refresh_proof_message(
                            UUID(registered["agent_id"]),
                            nonce,
                            timestamp,
                        )
                    )
                ),
            },
        )
        old_nonce = "b2xkLXJlZnJlc2gtbm9uY2UtMDAwMDAwMDA"
        rejected = client.post(
            "/v1/agent/token/refresh",
            json={
                "schema_version": "1.0",
                "agent_id": registered["agent_id"],
                "refresh_token": registered["refresh_token"],
                "nonce": old_nonce,
                "timestamp": timestamp,
                "signature_b64": encode_signature(
                    private_key.sign(
                        refresh_proof_message(
                            UUID(registered["agent_id"]),
                            old_nonce,
                            timestamp,
                        )
                    )
                ),
            },
        )

    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != registered["access_token"]
    assert refreshed.json()["refresh_token"] != registered["refresh_token"]
    assert rejected.status_code == 401
    assert registered["refresh_token"] not in rejected.text


def test_agent_endpoint_rejects_browser_cookie_before_registration() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    registration_code = "ppreg_" + "A" * 43
    with _client() as client:
        response = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": registration_code,
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "Ray Mac",
                "os_version": "macOS 15.6",
            },
            headers={"cookie": "perfpilot_session=session-token"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "agent_authentication_failed"
    assert registration_code not in response.text


def test_authenticated_heartbeat_publishes_only_sanitized_browser_device() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raw_serial = "R3CN30ABC7K2A"
    with _client() as client:
        issued = _create_code(client)
        registered = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "Ray Mac",
                "os_version": "macOS 15.6",
            },
        ).json()
        heartbeat_response = client.post(
            "/v1/agent/heartbeat",
            headers={"authorization": f"Bearer {registered['access_token']}"},
            json={
                "schema_version": "1.0",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "Ray Mac",
                "observed_at": "2026-08-05T08:00:00Z",
                "clock_skew_ms": 12,
                "disk_available_bytes": 107374182400,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [
                    {
                        "client_ref": "74000000-0000-4000-8000-000000000001",
                        "serial": raw_serial,
                        "manufacturer": "UNISOC",
                        "model": "ums9620",
                        "android_release": "15",
                        "api_level": 35,
                        "connection_type": "usb",
                        "adb_state": "device",
                        "battery_percent": 82,
                        "temperature_c": 31.5,
                        "storage_available_bytes": 42949672960,
                        "property_error_code": None,
                    }
                ],
            },
        )
        list_target = f"/v1/teams/{TEAM_A_ID}/devices"
        listed = _browser_request(
            client,
            method="GET",
            target=list_target,
            payload={},
            request_id="req-list-devices",
        )

    assert heartbeat_response.status_code == 200
    assert heartbeat_response.json()["devices"][0]["device_digest"]
    assert listed.status_code == 200
    assert listed.json()["devices"][0]["serial_suffix"] == "7K2A"
    assert listed.json()["devices"][0]["model"] == "ums9620"
    assert raw_serial not in heartbeat_response.text
    assert raw_serial not in listed.text
    assert heartbeat_response.json()["devices"][0]["device_digest"] not in listed.text


def test_heartbeat_v11_publishes_public_ready_source_workspace_directory() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    private_path = "/Users/ray/private/demo"
    with _client(source_code_analysis_enabled=True) as client:
        issued = _create_code(client)
        registered = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "Ray Mac",
                "os_version": "macOS 15.6",
            },
        ).json()
        heartbeat_response = client.post(
            "/v1/agent/heartbeat",
            headers={"authorization": f"Bearer {registered['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "Ray Mac",
                "observed_at": "2026-08-05T08:00:00Z",
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [
                    {
                        "workspace_id": "92000000-0000-4000-8000-000000000001",
                        "name": "Demo Android",
                        "state": "ready",
                        "git_branch": "main",
                        "git_head": "1" * 40,
                        "tracked_dirty_count": 1,
                        "snapshot_policy": "tracked_worktree",
                        "validation_profiles": [
                            {
                                "profile_id": "94000000-0000-4000-8000-000000000001",
                                "name": "Android check",
                            }
                        ],
                    }
                ],
            },
        )
        target = f"/v1/teams/{TEAM_A_ID}/source-workspaces"
        listed = _browser_request(
            client,
            method="GET",
            target=target,
            payload={},
            request_id="req-list-source-workspaces",
        )
        rejected_private = client.post(
            "/v1/agent/heartbeat",
            headers={"authorization": f"Bearer {registered['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "Ray Mac",
                "observed_at": "2026-08-05T08:00:00Z",
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [{**listed.json().get("workspaces", [{}])[0], "path": private_path}],
            },
        )

    assert heartbeat_response.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["workspaces"] == [
        {
            "provider_kind": "agent_workspace",
            "agent_id": registered["agent_id"],
            "agent_name": "Ray Mac",
            "workspace_id": "92000000-0000-4000-8000-000000000001",
            "name": "Demo Android",
            "state": "ready",
            "git_branch": "main",
            "git_head": "1" * 40,
            "tracked_dirty_count": 1,
            "snapshot_policy": "tracked_worktree",
            "validation_profiles": [
                {
                    "profile_id": "94000000-0000-4000-8000-000000000001",
                    "name": "Android check",
                }
            ],
        }
    ]
    assert rejected_private.status_code == 422
    assert private_path not in rejected_private.text


def test_heartbeat_v11_rejects_noncanonical_workspace_uuid() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    with _client(source_code_analysis_enabled=True) as client:
        issued = _create_code(client)
        registered = client.post(
            "/v1/agent/register",
            json={
                "schema_version": "1.0",
                "registration_code": issued["registration_code"],
                "public_key_b64": encode_ed25519_public_key(private_key.public_key()),
                "platform": "macos",
                "agent_version": "1.2.3",
                "hostname": "Ray Mac",
                "os_version": "macOS 15.6",
            },
        ).json()
        response = client.post(
            "/v1/agent/heartbeat",
            headers={"authorization": f"Bearer {registered['access_token']}"},
            json={
                "schema_version": "1.1",
                "agent_version": "1.2.3",
                "platform": "macos",
                "hostname": "Ray Mac",
                "observed_at": "2026-08-05T08:00:00Z",
                "clock_skew_ms": 0,
                "disk_available_bytes": 1024,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
                "workspaces": [
                    {
                        "workspace_id": "92000000-0000-4000-8000-00000000000A",
                        "name": "Demo Android",
                        "state": "ready",
                        "git_branch": None,
                        "git_head": "1" * 40,
                        "tracked_dirty_count": 0,
                        "snapshot_policy": "tracked_worktree",
                        "validation_profiles": [],
                    }
                ],
            },
        )

    assert response.status_code == 422


def test_heartbeat_rejects_missing_agent_access_token_without_proxy_headers() -> None:
    with _client() as client:
        response = client.post(
            "/v1/agent/heartbeat",
            json={
                "schema_version": "1.0",
                "agent_version": "1.2.3",
                "platform": "linux",
                "hostname": "Ubuntu Agent",
                "observed_at": "2026-08-05T08:00:00Z",
                "clock_skew_ms": 0,
                "disk_available_bytes": 1,
                "execution_slot": {"state": "idle", "execution_id": None},
                "devices": [],
            },
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "agent_authentication_failed"


def test_default_runtime_composes_sql_agent_service(monkeypatch) -> None:
    class FakeEngine:
        async def dispose(self) -> None:
            return None

    class FakeRedis:
        async def aclose(self) -> None:
            return None

    fake_sessions = object()
    fake_redis = FakeRedis()
    monkeypatch.setattr(main_module, "create_control_engine", lambda _: FakeEngine())
    monkeypatch.setattr(
        main_module,
        "create_control_session_factory",
        lambda _: fake_sessions,
    )
    monkeypatch.setattr(main_module.redis, "from_url", lambda _: fake_redis)

    app = create_app(
        testing=False,
        settings_override=Settings(app_env="development", _env_file=None),
        upload_service=object(),  # type: ignore[arg-type]
        analysis_service=object(),  # type: ignore[arg-type]
        memory_capture_service=object(),  # type: ignore[arg-type]
    )

    assert isinstance(app.state.agent_service, AgentService)
    assert isinstance(app.state.device_directory, DeviceDirectory)
