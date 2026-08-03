import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from perfpilot_api.config import Settings
from perfpilot_api.main import create_app
from perfpilot_api.security.proxy_signature import sign_proxy_request
from perfpilot_api.security.sessions import COOKIE_NAME
from perfpilot_api.services.analyses import (
    AnalysisIdempotencyConflictError,
    AnalysisInvalidRequestError,
    AnalysisNotFoundError,
    AnalysisQueueLimitError,
    AnalysisUnavailableError,
    AnalysisView,
    ApplicationMetadataView,
    ReportNotAvailableError,
    SampleVerdictCounts,
    ScenarioView,
)
from perfpilot_api.services.uploads import UploadSlot

TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("40000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("50000000-0000-4000-8000-000000000001")
APPLICATION_VERSION_ID = UUID("60000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
CHECKSUM = "iNQmb9TmM40TuEX88olXnVf6kQbc4EZhDbs8WjoWj4E="
PROXY_SECRET = "task7-analysis-proxy-secret"
ORIGIN = "https://console.example.com"
_ANALYSIS_RESPONSE_SCHEMA = Path(__file__).resolve().parents[4] / (
    "contracts/v1/analyses/analysis-response.schema.json"
)


class FakeAuthService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def authorize_team_request(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(user_id=USER_ID, team_id=TEAM_ID, role="team_member")


class FakeAnalysisService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.memory_requests: dict[str, tuple[UUID, str | None, AnalysisView]] = {}
        self.report: dict[str, object] = {
            "schema_version": "1.0",
            "analysis_id": str(ANALYSIS_ID),
            "analysis_mode": "device",
            "state": "completed",
            "report_version": 1,
            "generated_at": NOW.isoformat(),
            "scenario_reports": [],
        }

    async def create_device_analysis(self, **kwargs: object) -> AnalysisView:
        self.calls.append(("create", kwargs))
        if self.error is not None:
            raise self.error
        return _created_view()

    async def create_memory_analysis(self, **kwargs: object) -> AnalysisView:
        self.calls.append(("create_memory", kwargs))
        if self.error is not None:
            raise self.error
        question = kwargs["question"]
        normalized_question = str(question).strip() if question is not None else None
        if normalized_question == "":
            normalized_question = None
        if normalized_question is not None and len(normalized_question) > 2_000:
            raise AnalysisInvalidRequestError("analysis request is invalid")
        idempotency_key = str(kwargs["idempotency_key"])
        application_version_id = kwargs["application_version_id"]
        stored = self.memory_requests.get(idempotency_key)
        if stored is not None:
            stored_version_id, stored_question, view = stored
            if (
                stored_version_id != application_version_id
                or stored_question != normalized_question
            ):
                raise AnalysisIdempotencyConflictError()
            return view
        view = _memory_created_view(question=normalized_question)
        self.memory_requests[idempotency_key] = (
            application_version_id,  # type: ignore[arg-type]
            normalized_question,
            view,
        )
        return view

    async def get_analysis(self, **kwargs: object) -> AnalysisView:
        self.calls.append(("get", kwargs))
        if self.error is not None:
            raise self.error
        return _created_view(include_upload_authorization=False)

    async def get_report(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("report", kwargs))
        if self.error is not None:
            raise self.error
        return self.report

    async def finalize_upload(self, **kwargs: object) -> UploadSlot:
        self.calls.append(("finalize", kwargs))
        if self.error is not None:
            raise self.error
        return UploadSlot(
            artifact_id=ARTIFACT_ID,
            upload_id=UPLOAD_ID,
            artifact_kind="apk",
            mime="application/vnd.android.package-archive",
            size=4,
            sha256_b64=CHECKSUM,
            state="finalized",
            expires_at=NOW + timedelta(days=30),
            finalized_at=NOW,
            required_headers={},
            put_url=None,
            object_key="must-never-leave-the-service",
            version_id="must-never-leave-the-service",
        )


def _created_view(*, include_upload_authorization: bool = True) -> AnalysisView:
    slot = UploadSlot(
        artifact_id=ARTIFACT_ID,
        upload_id=UPLOAD_ID,
        artifact_kind="apk",
        mime="application/vnd.android.package-archive",
        size=4,
        sha256_b64=CHECKSUM,
        state="pending",
        expires_at=NOW + timedelta(minutes=15),
        finalized_at=None,
        required_headers=(
            {
                "Content-Type": "application/vnd.android.package-archive",
                "x-amz-checksum-sha256": CHECKSUM,
            }
            if include_upload_authorization
            else {}
        ),
        put_url=(
            "https://objects.example/upload-signature" if include_upload_authorization else None
        ),
        object_key="must-never-leave-the-service",
        version_id=None,
    )
    empty_counts = SampleVerdictCounts(
        valid=0,
        invalid=0,
        pending=0,
        validation_error=0,
        total=0,
    )
    scenarios = tuple(
        ScenarioView(
            scenario_job_id=None,
            scenario_type=scenario_type,  # type: ignore[arg-type]
            state="awaiting_input",
            version=None,
            device_group_id=None,
            sample_verdict_counts=empty_counts,
            started_at=None,
            completed_at=None,
            failure_code=None,
        )
        for scenario_type in ("cold_start", "scroll", "memory_cycle")
    )
    return AnalysisView(
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        analysis_mode="device",
        state="created",
        version=2,
        application_version_id=None,
        application_metadata=None,
        apk_upload=slot,
        scenarios=scenarios,
        sample_verdict_counts=empty_counts,
        active_lease=None,
        report_available=False,
        created_at=NOW,
        started_at=None,
        completed_at=None,
        failure_code=None,
    )


def _memory_created_view(*, question: str | None) -> AnalysisView:
    empty_counts = SampleVerdictCounts(
        valid=0,
        invalid=0,
        pending=0,
        validation_error=0,
        total=0,
    )
    return AnalysisView(
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        analysis_mode="memory_upload",
        state="created",
        version=2,
        application_version_id=APPLICATION_VERSION_ID,
        application_metadata=ApplicationMetadataView(
            package_name="dev.perfpilot.demo",
            version_name="1.2.3",
            version_code=12,
            launch_activity="dev.perfpilot.demo.MainActivity",
            min_sdk=28,
            target_sdk=35,
            supported_abis=("arm64-v8a",),
            has_native_libraries=False,
        ),
        apk_upload=None,
        scenarios=(),
        sample_verdict_counts=empty_counts,
        active_lease=None,
        report_available=False,
        created_at=NOW,
        started_at=None,
        completed_at=None,
        failure_code=None,
        question=question,
    )


def _settings() -> Settings:
    return Settings(
        app_env="test",
        proxy_secret=PROXY_SECRET,
        allowed_origins=[ORIGIN],
        _env_file=None,
        _secrets_dir=None,
    )


def _headers(*, method: str, target: str, body: bytes, request_id: str) -> dict[str, str]:
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
    analysis_service: FakeAnalysisService,
) -> TestClient:
    return TestClient(
        create_app(
            testing=True,
            settings_override=_settings(),
            auth_service=auth_service,  # type: ignore[arg-type]
            analysis_service=analysis_service,  # type: ignore[arg-type]
            proxy_clock=lambda: 1_700_000_000,
        )
    )


def _create_body() -> bytes:
    return json.dumps(
        {
            "schema_version": "1.0",
            "analysis_mode": "device",
            "scenarios": ["cold_start", "scroll", "memory_cycle"],
            "apk": {
                "artifact_kind": "apk",
                "mime": "application/vnd.android.package-archive",
                "size": 4,
                "sha256_b64": CHECKSUM,
            },
        },
        separators=(",", ":"),
    ).encode()


def _memory_create_body(
    *,
    question: str | None = "  退出页面后内存没有下降  ",
    extra: dict[str, object] | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "analysis_mode": "memory_upload",
        "application_version_id": str(APPLICATION_VERSION_ID),
        "question": question,
    }
    if extra is not None:
        payload.update(extra)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def test_create_memory_analysis_returns_private_question_without_upload_or_scenarios() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses"
    body = _memory_create_body()
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-memory-analysis-create",
    )
    headers["Idempotency-Key"] = "memory-analysis-1"

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 201
    Draft202012Validator(
        json.loads(_ANALYSIS_RESPONSE_SCHEMA.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    ).validate(response.json())
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["analysis_mode"] == "memory_upload"
    assert response.json()["application_version_id"] == str(APPLICATION_VERSION_ID)
    assert response.json()["question"] == "退出页面后内存没有下降"
    assert response.json()["apk_upload"] is None
    assert response.json()["scenarios"] == []
    assert "put_url" not in response.text
    assert analysis_service.calls == [
        (
            "create_memory",
            {
                "team_id": TEAM_ID,
                "requested_by_user_id": USER_ID,
                "idempotency_key": "memory-analysis-1",
                "application_version_id": APPLICATION_VERSION_ID,
                "question": "  退出页面后内存没有下降  ",
            },
        )
    ]


def test_create_memory_analysis_replays_and_rejects_idempotency_mismatch() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses"
    first_body = _memory_create_body(question="retained objects")
    first_headers = _headers(
        method="POST",
        target=target,
        body=first_body,
        request_id="req-memory-analysis-first",
    )
    first_headers["Idempotency-Key"] = "memory-analysis-replay"
    replay_headers = _headers(
        method="POST",
        target=target,
        body=first_body,
        request_id="req-memory-analysis-replay",
    )
    replay_headers["Idempotency-Key"] = "memory-analysis-replay"
    mismatch_body = _memory_create_body(question="different question")
    mismatch_headers = _headers(
        method="POST",
        target=target,
        body=mismatch_body,
        request_id="req-memory-analysis-mismatch",
    )
    mismatch_headers["Idempotency-Key"] = "memory-analysis-replay"

    with _client(auth_service, analysis_service) as client:
        first = client.post(target, content=first_body, headers=first_headers)
        replay = client.post(target, content=first_body, headers=replay_headers)
        mismatch = client.post(target, content=mismatch_body, headers=mismatch_headers)

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_conflict"


def test_create_memory_analysis_normalizes_oversized_blank_question_to_none() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses"
    question = " " * 2_001
    body = _memory_create_body(question=question)
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-memory-analysis-oversized-blank",
    )
    headers["Idempotency-Key"] = "memory-analysis-oversized-blank"

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 201
    assert response.json()["question"] is None
    assert analysis_service.calls == [
        (
            "create_memory",
            {
                "team_id": TEAM_ID,
                "requested_by_user_id": USER_ID,
                "idempotency_key": "memory-analysis-oversized-blank",
                "application_version_id": APPLICATION_VERSION_ID,
                "question": question,
            },
        )
    ]


def test_create_memory_analysis_maps_oversized_normalized_question_to_stable_error() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses"
    question = f" {'x' * 2_001} "
    body = _memory_create_body(question=question)
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-memory-analysis-oversized-question",
    )
    headers["Idempotency-Key"] = "memory-analysis-oversized-question"

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert analysis_service.calls == [
        (
            "create_memory",
            {
                "team_id": TEAM_ID,
                "requested_by_user_id": USER_ID,
                "idempotency_key": "memory-analysis-oversized-question",
                "application_version_id": APPLICATION_VERSION_ID,
                "question": question,
            },
        )
    ]


def test_create_memory_analysis_rejects_extra_fields_and_missing_discriminator() -> None:
    cases = (
        _memory_create_body(extra={"apk": {}}),
        json.dumps(
            {
                "schema_version": "1.0",
                "application_version_id": str(APPLICATION_VERSION_ID),
            },
            separators=(",", ":"),
        ).encode(),
    )
    for index, body in enumerate(cases):
        auth_service = FakeAuthService()
        analysis_service = FakeAnalysisService()
        target = f"/v1/teams/{TEAM_ID}/analyses"
        headers = _headers(
            method="POST",
            target=target,
            body=body,
            request_id=f"req-memory-analysis-invalid-{index}",
        )
        headers["Idempotency-Key"] = "memory-analysis-invalid"

        with _client(auth_service, analysis_service) as client:
            response = client.post(target, content=body, headers=headers)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_failed"
        assert analysis_service.calls == []


def test_create_memory_analysis_cross_tenant_version_returns_stable_not_found() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    analysis_service.error = AnalysisNotFoundError("application version was not found")
    target = f"/v1/teams/{TEAM_ID}/analyses"
    body = _memory_create_body()
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-memory-analysis-cross-tenant",
    )
    headers["Idempotency-Key"] = "memory-analysis-cross-tenant"

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert str(APPLICATION_VERSION_ID) not in response.text


def test_create_device_analysis_returns_pending_apk_slot_without_internal_locations() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses"
    body = _create_body()
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-analysis-create",
    )
    headers["Idempotency-Key"] = "device-analysis-1"

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 201
    Draft202012Validator(
        json.loads(_ANALYSIS_RESPONSE_SCHEMA.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    ).validate(response.json())
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schema_version": "1.0",
        "analysis_id": str(ANALYSIS_ID),
        "team_id": str(TEAM_ID),
        "analysis_mode": "device",
        "state": "created",
        "version": 2,
        "application_version_id": None,
        "application_metadata": None,
        "apk_upload": {
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
        "scenarios": [
            {
                "scenario_job_id": None,
                "scenario_type": scenario_type,
                "state": "awaiting_input",
                "version": None,
                "device_group_id": None,
                "sample_verdict_counts": {
                    "valid": 0,
                    "invalid": 0,
                    "pending": 0,
                    "validation_error": 0,
                    "total": 0,
                },
                "started_at": None,
                "completed_at": None,
                "failure": None,
            }
            for scenario_type in ("cold_start", "scroll", "memory_cycle")
        ],
        "sample_verdict_counts": {
            "valid": 0,
            "invalid": 0,
            "pending": 0,
            "validation_error": 0,
            "total": 0,
        },
        "active_lease": None,
        "report_available": False,
        "created_at": "2026-07-28T08:00:00+00:00",
        "started_at": None,
        "completed_at": None,
        "failure": None,
    }
    assert "must-never-leave-the-service" not in response.text
    assert analysis_service.calls == [
        (
            "create",
            {
                "team_id": TEAM_ID,
                "requested_by_user_id": USER_ID,
                "idempotency_key": "device-analysis-1",
                "scenarios": ("cold_start", "scroll", "memory_cycle"),
                "apk_mime": "application/vnd.android.package-archive",
                "apk_size": 4,
                "apk_sha256_b64": CHECKSUM,
            },
        )
    ]


def test_create_requires_exact_fixed_scenarios_and_one_idempotency_header() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses"
    payload = json.loads(_create_body())
    payload["scenarios"] = ["cold_start"]
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-analysis-invalid-scenarios",
    )
    headers["Idempotency-Key"] = "device-analysis-1"

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert analysis_service.calls == []


def test_idempotency_conflict_and_queue_limit_have_stable_errors() -> None:
    cases = (
        (AnalysisIdempotencyConflictError(), 409, "idempotency_conflict"),
        (AnalysisQueueLimitError(), 429, "team_queue_limit"),
    )
    for index, (error, status, code) in enumerate(cases):
        auth_service = FakeAuthService()
        analysis_service = FakeAnalysisService()
        analysis_service.error = error
        target = f"/v1/teams/{TEAM_ID}/analyses"
        body = _create_body()
        headers = _headers(
            method="POST",
            target=target,
            body=body,
            request_id=f"req-analysis-error-{index}",
        )
        headers["Idempotency-Key"] = "device-analysis-1"

        with _client(auth_service, analysis_service) as client:
            response = client.post(target, content=body, headers=headers)

        assert response.status_code == status
        assert response.json()["error"]["code"] == code


def test_get_analysis_does_not_reissue_the_pending_upload_authorization() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}"
    headers = _headers(
        method="GET",
        target=target,
        body=b"",
        request_id="req-analysis-get",
    )
    headers.pop("content-type")

    with _client(auth_service, analysis_service) as client:
        response = client.get(target, headers=headers)

    assert response.status_code == 200
    assert response.json()["apk_upload"]["state"] == "pending"
    assert "put_url" not in response.json()["apk_upload"]
    assert "required_headers" not in response.json()["apk_upload"]
    assert analysis_service.calls == [("get", {"team_id": TEAM_ID, "analysis_id": ANALYSIS_ID})]


def test_finalize_route_uses_authoritative_analysis_upload_classification() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}/finalize-upload"
    body = json.dumps(
        {"upload_id": str(UPLOAD_ID), "sha256_b64": CHECKSUM, "size": 4},
        separators=(",", ":"),
    ).encode()
    headers = _headers(
        method="POST",
        target=target,
        body=body,
        request_id="req-analysis-finalize",
    )

    with _client(auth_service, analysis_service) as client:
        response = client.post(target, content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["upload"]["state"] == "finalized"
    assert analysis_service.calls == [
        (
            "finalize",
            {
                "team_id": TEAM_ID,
                "analysis_id": ANALYSIS_ID,
                "upload_id": UPLOAD_ID,
                "caller_sha256_b64": CHECKSUM,
                "caller_size": 4,
            },
        )
    ]
    assert "must-never-leave-the-service" not in response.text


def test_analysis_and_report_not_found_errors_do_not_leak_cross_team_existence() -> None:
    cases = (
        ("get", AnalysisNotFoundError(), "resource_not_found"),
        ("report", ReportNotAvailableError(), "report_not_available"),
    )
    for index, (kind, error, code) in enumerate(cases):
        auth_service = FakeAuthService()
        analysis_service = FakeAnalysisService()
        analysis_service.error = error
        suffix = "/report" if kind == "report" else ""
        target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}{suffix}"
        headers = _headers(
            method="GET",
            target=target,
            body=b"",
            request_id=f"req-analysis-not-found-{index}",
        )
        headers.pop("content-type")

        with _client(auth_service, analysis_service) as client:
            response = client.get(target, headers=headers)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == code
        assert "database" not in response.text.lower()
        assert "bucket" not in response.text.lower()


def test_unavailable_analysis_error_does_not_leak_private_question() -> None:
    auth_service = FakeAuthService()
    analysis_service = FakeAnalysisService()
    private_question = "which retained object contains customer-secret-42"
    analysis_service.error = AnalysisUnavailableError(private_question)
    target = f"/v1/teams/{TEAM_ID}/analyses/{ANALYSIS_ID}"
    headers = _headers(
        method="GET",
        target=target,
        body=b"",
        request_id="req-analysis-deleted-private-question",
    )
    headers.pop("content-type")

    with _client(auth_service, analysis_service) as client:
        response = client.get(target, headers=headers)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert private_question not in response.text


def test_application_metadata_is_server_owned() -> None:
    metadata = ApplicationMetadataView(
        package_name="dev.perfpilot.demo",
        version_name="1.2.3",
        version_code=12,
        launch_activity="dev.perfpilot.demo.MainActivity",
        min_sdk=28,
        target_sdk=35,
        supported_abis=("arm64-v8a",),
        has_native_libraries=False,
    )
    assert metadata.package_name == "dev.perfpilot.demo"
