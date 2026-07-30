from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from perfpilot_api.config import Settings
from perfpilot_api.engines.android_memory_contracts import MemoryCaptureManifest
from perfpilot_api.main import create_app
from perfpilot_api.security.proxy_signature import sign_proxy_request
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.auth import (
    InvalidSessionError,
    RoleForbiddenError,
    TeamAccessNotFoundError,
)
from perfpilot_api.services.internal_artifacts import manifest_artifact_id
from perfpilot_api.services.memory_analyses import (
    CreatedMemoryCapture,
    MemoryCaptureConflictError,
    MemoryCaptureInvalidRequestError,
    MemoryCaptureNotFoundError,
    MemoryCaptureUnavailableError,
)


TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
CAPTURE_ID = UUID("40000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("50000000-0000-4000-8000-000000000001")
PROXY_SECRET = "task5-memory-capture-proxy-secret"
ORIGIN = "https://console.example.com"


class FakeAuthService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def authorize_team_request(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(user_id=USER_ID, team_id=TEAM_ID, role="team_member")


class FakeMemoryCaptureService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None
        self.created: CreatedMemoryCapture | None = None

    async def create_capture(self, **kwargs: object) -> CreatedMemoryCapture:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        manifest = MemoryCaptureManifest(
            schema_version="1.0",
            analysis_id=ANALYSIS_ID,
            capture_id=CAPTURE_ID,
            phase=kwargs["phase"],  # type: ignore[arg-type]
            source=kwargs["source"],  # type: ignore[arg-type]
            captured_at=kwargs["captured_at"],  # type: ignore[arg-type]
            subject=kwargs["subject"],  # type: ignore[arg-type]
            artifacts=kwargs["artifacts"],  # type: ignore[arg-type]
        )
        self.created = CreatedMemoryCapture(
            artifact_id=manifest_artifact_id(CAPTURE_ID),
            manifest=manifest,
            manifest_sha256=manifest.sha256_hex(),
        )
        return self.created


def _settings() -> Settings:
    return Settings(
        app_env="test",
        proxy_secret=PROXY_SECRET,
        allowed_origins=[ORIGIN],
        _env_file=None,
        _secrets_dir=None,
    )


def _headers(*, body: bytes, request_id: str) -> dict[str, str]:
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/memory-captures"
    signature = sign_proxy_request(
        PROXY_SECRET.encode(),
        timestamp=1_700_000_000,
        request_id=request_id,
        method="POST",
        raw_path=target.encode("ascii"),
        raw_query=b"",
        body=body,
    )
    return {
        "x-perfpilot-proxy-timestamp": "1700000000",
        "x-perfpilot-proxy-signature": signature,
        "x-request-id": request_id,
        "origin": ORIGIN,
        "x-csrf-token": "csrf-token",
        "cookie": f"{COOKIE_NAME}=session-token",
        "content-type": "application/json",
    }


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "phase": "single",
        "source": "manual_upload",
        "captured_at": None,
        "subject": {"package": "com.example.app", "android_sdk": 37},
        "artifacts": [{"artifact_id": str(EVIDENCE_ID), "role": "meminfo"}],
    }
    payload.update(changes)
    return payload


def _client(
    auth_service: FakeAuthService,
    memory_capture_service: FakeMemoryCaptureService,
) -> TestClient:
    return TestClient(
        create_app(
            testing=True,
            settings_override=_settings(),
            auth_service=auth_service,  # type: ignore[arg-type]
            memory_capture_service=memory_capture_service,  # type: ignore[arg-type]
            proxy_clock=lambda: 1_700_000_000,
        )
    )


def _post(
    client: TestClient,
    payload: dict[str, object],
    *,
    request_id: str,
) -> object:
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/memory-captures"
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(target, content=body, headers=_headers(body=body, request_id=request_id))


def test_authenticated_user_creates_manual_capture_with_no_store_response() -> None:
    auth = FakeAuthService()
    service = FakeMemoryCaptureService()
    with _client(auth, service) as client:
        response = _post(client, _payload(), request_id="req-memory-capture")

    assert response.status_code == 201  # type: ignore[attr-defined]
    assert response.headers["cache-control"] == "no-store"  # type: ignore[attr-defined]
    assert service.created is not None
    assert response.json() == {  # type: ignore[attr-defined]
        "schema_version": "1.0",
        "capture_id": str(CAPTURE_ID),
        "manifest_artifact_id": str(manifest_artifact_id(CAPTURE_ID)),
        "manifest_sha256": service.created.manifest_sha256,
        "state": "created",
    }
    assert auth.calls == [
        {
            "session_token": "session-token",
            "csrf_token": "csrf-token",
            "team_id": TEAM_ID,
            "access": "write",
        }
    ]
    assert service.calls[0]["team_id"] == TEAM_ID
    assert service.calls[0]["analysis_id"] == ANALYSIS_ID
    assert service.calls[0]["source"] == "manual_upload"


@pytest.mark.parametrize(
    "change",
    [
        {"source": "adb_agent"},
        {"unexpected": "field"},
        {"schema_version": "2.0"},
        {"phase": "unknown"},
        {"artifacts": []},
        {"captured_at": 1_700_000_000},
        {"captured_at": "2026-07-29T08:00:00"},
        {"captured_at": "2026-07-29T16:00:00+08:00"},
        {"subject": {"package": "com.example.app", "unknown": True}},
        {"artifacts": [{"artifact_id": str(EVIDENCE_ID), "role": "unknown"}]},
    ],
)
def test_strict_body_rejects_invalid_or_agent_owned_inputs_without_side_effects(
    change: dict[str, object],
) -> None:
    auth = FakeAuthService()
    service = FakeMemoryCaptureService()
    with _client(auth, service) as client:
        response = _post(client, _payload(**change), request_id=f"req-invalid-{len(change)}")

    assert response.status_code == 422  # type: ignore[attr-defined]
    assert response.json()["error"]["code"] == "request_validation_failed"  # type: ignore[attr-defined]
    assert auth.calls == []
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidSessionError(), 401, "unauthenticated"),
        (TeamAccessNotFoundError(), 404, "resource_not_found"),
        (RoleForbiddenError(), 403, "role_forbidden"),
    ],
)
def test_auth_and_tenant_failures_precede_capture_service(
    error: Exception,
    status: int,
    code: str,
) -> None:
    auth = FakeAuthService()
    auth.error = error
    service = FakeMemoryCaptureService()
    with _client(auth, service) as client:
        response = _post(client, _payload(), request_id=f"req-auth-{status}")

    assert response.status_code == status  # type: ignore[attr-defined]
    assert response.json()["error"]["code"] == code  # type: ignore[attr-defined]
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            MemoryCaptureInvalidRequestError("private-metadata-marker"),
            422,
            "request_validation_failed",
        ),
        (MemoryCaptureNotFoundError("private-bucket-marker"), 404, "resource_not_found"),
        (MemoryCaptureConflictError("private-checksum-marker"), 409, "idempotency_conflict"),
        (MemoryCaptureUnavailableError("private-object-key-marker"), 503, "service_unavailable"),
    ],
)
def test_capture_errors_have_stable_redacted_api_mapping(
    error: Exception,
    status: int,
    code: str,
) -> None:
    auth = FakeAuthService()
    service = FakeMemoryCaptureService()
    service.error = error
    with _client(auth, service) as client:
        response = _post(client, _payload(), request_id=f"req-service-{status}")

    assert response.status_code == status  # type: ignore[attr-defined]
    assert response.json()["error"]["code"] == code  # type: ignore[attr-defined]
    assert "private-" not in response.text  # type: ignore[attr-defined]


def test_more_than_2048_artifacts_is_rejected_before_authentication() -> None:
    auth = FakeAuthService()
    service = FakeMemoryCaptureService()
    artifacts = [{"artifact_id": str(UUID(int=index + 1)), "role": "auto"} for index in range(2049)]
    with _client(auth, service) as client:
        response = _post(
            client,
            _payload(artifacts=artifacts),
            request_id="req-too-many-artifacts",
        )

    assert response.status_code == 422  # type: ignore[attr-defined]
    assert auth.calls == []
    assert service.calls == []
