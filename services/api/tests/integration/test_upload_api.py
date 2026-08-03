import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.security.proxy_signature import sign_proxy_request
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.auth import RoleForbiddenError, TeamAccessNotFoundError
from perfpilot_api.services.uploads import (
    DownloadAuthorization,
    UploadIdempotencyConflictError,
    UploadInvalidRequestError,
    UploadSlot,
)

TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("40000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("50000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
CHECKSUM = "iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E="
PROXY_SECRET = "task6-upload-proxy-secret"
ORIGIN = "https://console.example.com"
_CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "v1" / "artifacts"


def _validate_contract(name: str, payload: object) -> None:
    schema = json.loads((_CONTRACT_ROOT / name).read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


class FakeAuthService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def authorize_team_request(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(user_id=UUID(int=1), team_id=TEAM_ID, role="team_member")


class FakeUploadService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.pending = UploadSlot(
            artifact_id=ARTIFACT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind="apk",
            mime="application/vnd.android.package-archive",
            size=4,
            sha256_b64=CHECKSUM,
            state="pending",
            expires_at=NOW + timedelta(minutes=15),
            finalized_at=None,
            required_headers={
                "Content-Type": "application/vnd.android.package-archive",
                "x-amz-checksum-sha256": CHECKSUM,
            },
            put_url="https://objects.example/upload-signature",
            object_key="must-never-leave-the-service",
            version_id=None,
        )
        self.finalized = UploadSlot(
            artifact_id=ARTIFACT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind="apk",
            mime="application/vnd.android.package-archive",
            size=4,
            sha256_b64=CHECKSUM,
            state="finalized",
            expires_at=NOW + timedelta(minutes=15),
            finalized_at=NOW + timedelta(minutes=1),
            required_headers={},
            put_url=None,
            object_key="must-never-leave-the-service",
            version_id="must-never-leave-the-service",
        )

    async def create_slot(self, **kwargs: object) -> UploadSlot:
        self.calls.append(("create", kwargs))
        if self.error is not None:
            raise self.error
        if kwargs["artifact_kind"] == "memory_capture_manifest":
            raise UploadInvalidRequestError("upload request is invalid")
        return replace(
            self.pending,
            artifact_kind=kwargs["artifact_kind"],  # type: ignore[arg-type]
            mime=kwargs["mime"],  # type: ignore[arg-type]
            size=kwargs["size"],  # type: ignore[arg-type]
            sha256_b64=kwargs["sha256_b64"],  # type: ignore[arg-type]
        )

    async def finalize(self, **kwargs: object) -> UploadSlot:
        self.calls.append(("finalize", kwargs))
        if self.error is not None:
            raise self.error
        return self.finalized

    async def download(self, **kwargs: object) -> DownloadAuthorization:
        self.calls.append(("download", kwargs))
        if self.error is not None:
            raise self.error
        return DownloadAuthorization(
            artifact_id=ARTIFACT_ID,
            tenant_resource_version=1,
            artifact_version=2,
            artifact_kind="trace",
            mime="application/octet-stream",
            size=128,
            sha256_b64="YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
            url="https://objects.example/download-signature",
            expires_at=NOW + timedelta(minutes=5),
        )


def _settings() -> Settings:
    return Settings(
        app_env="test",
        proxy_secret=PROXY_SECRET,
        allowed_origins=[ORIGIN],
        _env_file=None,
        _secrets_dir=None,
    )


def _proxy_headers(
    *,
    method: str,
    target: str,
    body: bytes,
    request_id: str,
) -> dict[str, str]:
    signature = sign_proxy_request(
        PROXY_SECRET.encode(),
        timestamp=1_700_000_000,
        request_id=request_id,
        method=method,
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


def _client(
    auth_service: FakeAuthService,
    upload_service: FakeUploadService,
) -> TestClient:
    return TestClient(
        create_app(
            testing=True,
            settings_override=_settings(),
            auth_service=auth_service,  # type: ignore[arg-type]
            upload_service=upload_service,  # type: ignore[arg-type]
            proxy_clock=lambda: 1_700_000_000,
        )
    )


def test_create_upload_slot_returns_only_the_public_pending_contract() -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/uploads"
    body = json.dumps(
        {
            "artifact_kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 4,
            "sha256_b64": CHECKSUM,
        },
        separators=(",", ":"),
    ).encode()
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-upload-create",
    )
    headers["Idempotency-Key"] = "analysis-apk-1"
    _validate_contract("slot-request.schema.json", json.loads(body))

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 201
    _validate_contract("slot-response.schema.json", response.json())
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schema_version": "1.0",
        "upload": {
            "state": "pending",
            "upload_id": str(UPLOAD_ID),
            "artifact_kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 4,
            "sha256_b64": CHECKSUM,
            "expires_at": "2026-07-28T08:15:00+00:00",
            "put_url": "https://objects.example/upload-signature",
            "required_headers": {
                "Content-Type": "application/vnd.android.package-archive",
                "x-amz-checksum-sha256": CHECKSUM,
            },
        },
    }
    assert auth_service.calls == [
        {
            "session_token": "session-token",
            "csrf_token": "csrf-token",
            "team_id": TEAM_ID,
            "access": "write",
        }
    ]
    assert upload_service.calls == [
        (
            "create",
            {
                "team_id": TEAM_ID,
                "analysis_id": ANALYSIS_ID,
                "idempotency_key": "analysis-apk-1",
                "artifact_kind": "apk",
                "mime": "application/vnd.android.package-archive",
                "size": 4,
                "sha256_b64": CHECKSUM,
            },
        )
    ]
    assert "must-never-leave-the-service" not in response.text


@pytest.mark.parametrize("artifact_kind", ["memory_evidence", "screenshot"])
def test_memory_input_kinds_receive_normal_authenticated_reservations(
    artifact_kind: str,
) -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/uploads"
    body = json.dumps(
        {
            "artifact_kind": artifact_kind,
            "mime": "application/octet-stream",
            "size": 128,
            "sha256_b64": CHECKSUM,
        },
        separators=(",", ":"),
    ).encode()
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id=f"req-{artifact_kind}",
    )
    headers["Idempotency-Key"] = f"memory-{artifact_kind}"

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 201
    assert response.json()["upload"]["artifact_kind"] == artifact_kind
    assert auth_service.calls[0]["access"] == "write"
    assert upload_service.calls == [
        (
            "create",
            {
                "team_id": TEAM_ID,
                "analysis_id": ANALYSIS_ID,
                "idempotency_key": f"memory-{artifact_kind}",
                "artifact_kind": artifact_kind,
                "mime": "application/octet-stream",
                "size": 128,
                "sha256_b64": CHECKSUM,
            },
        )
    ]


def test_memory_capture_manifest_is_rejected_with_stable_public_error() -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/uploads"
    body = json.dumps(
        {
            "artifact_kind": "memory_capture_manifest",
            "mime": "application/octet-stream",
            "size": 128,
            "sha256_b64": CHECKSUM,
        },
        separators=(",", ":"),
    ).encode()
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-memory-capture-manifest",
    )
    headers["Idempotency-Key"] = "memory-capture-manifest"

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert "memory_capture_manifest" not in response.text
    assert auth_service.calls[0]["access"] == "write"
    assert upload_service.calls[0][0] == "create"


def test_finalize_upload_uses_the_fixed_analysis_route_and_hides_storage_version() -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/finalize-upload"
    body = json.dumps(
        {"upload_id": str(UPLOAD_ID), "sha256_b64": CHECKSUM, "size": 4},
        separators=(",", ":"),
    ).encode()
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-upload-finalize",
    )
    _validate_contract("finalize-request.schema.json", json.loads(body))

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 200
    _validate_contract("slot-response.schema.json", response.json())
    assert response.json() == {
        "schema_version": "1.0",
        "upload": {
            "state": "finalized",
            "artifact_id": str(ARTIFACT_ID),
            "upload_id": str(UPLOAD_ID),
            "artifact_kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 4,
            "sha256_b64": CHECKSUM,
            "finalized_at": "2026-07-28T08:01:00+00:00",
        },
    }
    assert "must-never-leave-the-service" not in response.text


def test_download_is_membership_bound_and_returns_a_nosniff_versioned_authorization() -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/artifacts/{ARTIFACT_ID}/download"
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=b"",
        request_id="req-upload-download",
    )
    headers.pop("content-type")

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=b"", headers=headers)

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "schema_version": "1.0",
        "download": {
            "artifact_id": str(ARTIFACT_ID),
            "url": "https://objects.example/download-signature",
            "expires_at": "2026-07-28T08:05:00+00:00",
        },
    }
    assert auth_service.calls[0]["access"] == "read"


def test_duplicate_idempotency_headers_are_rejected_before_authorization() -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/uploads"
    body = json.dumps(
        {
            "artifact_kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 4,
            "sha256_b64": CHECKSUM,
        },
        separators=(",", ":"),
    ).encode()
    signed = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-upload-duplicate-idempotency",
    )
    headers = list(signed.items()) + [
        ("Idempotency-Key", "first"),
        ("Idempotency-Key", "second"),
    ]

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert auth_service.calls == []
    assert upload_service.calls == []


def test_upload_errors_map_to_stable_non_enumerating_codes() -> None:
    cases = [
        (TeamAccessNotFoundError(), 404, "resource_not_found"),
        (RoleForbiddenError(), 403, "role_forbidden"),
        (UploadIdempotencyConflictError(), 409, "idempotency_conflict"),
    ]
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/uploads"
    body = json.dumps(
        {
            "artifact_kind": "apk",
            "mime": "application/vnd.android.package-archive",
            "size": 4,
            "sha256_b64": CHECKSUM,
        },
        separators=(",", ":"),
    ).encode()

    for index, (error, status_code, code) in enumerate(cases):
        auth_service = FakeAuthService()
        upload_service = FakeUploadService()
        if isinstance(error, (TeamAccessNotFoundError, RoleForbiddenError)):
            auth_service.error = error
        else:
            upload_service.error = error
        headers = _proxy_headers(
            method="POST",
            target=target,
            body=body,
            request_id=f"req-upload-error-{index}",
        )
        headers["Idempotency-Key"] = "analysis-apk-1"

        with _client(auth_service, upload_service) as client:
            response = client.post(target, content=body, headers=headers)

        assert response.status_code == status_code
        assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("change", "idempotency_key"),
    [
        ({"size": "4"}, "analysis-apk-1"),
        ({"size": True}, "analysis-apk-1"),
        ({"artifact_kind": "../apk"}, "analysis-apk-1"),
        ({"mime": "Application/ZIP"}, "analysis-apk-1"),
        ({"sha256_b64": "A" * 44}, "analysis-apk-1"),
        ({"unexpected": "field"}, "analysis-apk-1"),
        ({}, "contains/slash"),
    ],
)
def test_schema_invalid_slot_requests_are_rejected_without_side_effects(
    change: dict[str, object],
    idempotency_key: str,
) -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/uploads"
    payload: dict[str, object] = {
        "artifact_kind": "apk",
        "mime": "application/vnd.android.package-archive",
        "size": 4,
        "sha256_b64": CHECKSUM,
    }
    payload.update(change)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id=f"req-invalid-slot-{len(change)}-{len(idempotency_key)}",
    )
    headers["Idempotency-Key"] = idempotency_key

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert auth_service.calls == []
    assert upload_service.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"size": "4"},
        {"size": True},
        {"sha256_b64": "A" * 44},
        {"unexpected": "field"},
    ],
)
def test_schema_invalid_finalize_requests_are_rejected_without_side_effects(
    change: dict[str, object],
) -> None:
    auth_service = FakeAuthService()
    upload_service = FakeUploadService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/finalize-upload"
    payload: dict[str, object] = {
        "upload_id": str(UPLOAD_ID),
        "sha256_b64": CHECKSUM,
        "size": 4,
    }
    payload.update(change)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _proxy_headers(
        method="POST",
        target=target,
        body=body,
        request_id=f"req-invalid-finalize-{len(change)}",
    )

    with _client(auth_service, upload_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert auth_service.calls == []
    assert upload_service.calls == []
