from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from perfpilot_api.engines.smartperfetto_contracts import (
    SmartPerfettoAnalyzeRequest,
    SmartPerfettoAnalyzeResponse,
    SmartPerfettoCancelResponse,
    SmartPerfettoEndpointError,
    SmartPerfettoReportResponse,
    SmartPerfettoResumeResponse,
    SmartPerfettoScenePreview,
    SmartPerfettoStatusResponse,
    SmartPerfettoTraceUploadResponse,
    SmartPerfettoWorkspaceCreateResponse,
    SmartPerfettoWorkspaceListResponse,
)


_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "smartperfetto_workspace_agent_v1"
)


def _json_fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _sse_data_fixture(name: str, event_name: str) -> dict[str, object]:
    current_event: str | None = None
    data_lines: list[str] = []
    for line in (_FIXTURES / name).read_text(encoding="utf-8").splitlines() + [""]:
        if not line:
            if current_event == event_name and data_lines:
                return json.loads("\n".join(data_lines))
            current_event = None
            data_lines = []
        elif line.startswith("event:"):
            current_event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    raise AssertionError(f"fixture event not found: {event_name}")


def test_fixture_manifest_freezes_the_consumer_owned_contract() -> None:
    manifest = (_FIXTURES / "README.md").read_text(encoding="utf-8")

    assert "Upstream: Gracker/SmartPerfetto" in manifest
    assert "Tag: v1.0.38" in manifest
    assert "Commit: 1508f99788bfcf18cc861e4bf4f8b472e84240c3" in manifest
    assert "Contract owner: PerfPilot" in manifest
    assert "Contract name: workspace-agent-v1" in manifest
    assert "Upstream handshake: none" in manifest


def test_workspace_responses_require_success_and_usable_ids() -> None:
    listed = SmartPerfettoWorkspaceListResponse.model_validate(
        _json_fixture("workspace-list-success.json")
    )
    created = SmartPerfettoWorkspaceCreateResponse.model_validate(
        _json_fixture("workspace-create-success.json")
    )

    assert [workspace.id for workspace in listed.workspaces] == [
        "pp-11111111-2222-5333-8444-555555555555"
    ]
    assert created.workspace.id == "pp-11111111-2222-5333-8444-555555555555"

    for response_type, payload in (
        (SmartPerfettoWorkspaceListResponse, {"success": False, "workspaces": []}),
        (
            SmartPerfettoWorkspaceCreateResponse,
            {"success": False, "workspace": {"id": "workspace-rejected"}},
        ),
        (
            SmartPerfettoWorkspaceCreateResponse,
            {"success": True, "workspace": {"id": " "}},
        ),
    ):
        with pytest.raises(ValidationError):
            response_type.model_validate(payload)


def test_upload_rejects_http_success_body_whose_contract_success_is_false() -> None:
    uploaded = SmartPerfettoTraceUploadResponse.model_validate(
        _json_fixture("trace-upload-success.json")
    )

    assert uploaded.trace.id == "trace-synthetic-001"
    with pytest.raises(ValidationError):
        SmartPerfettoTraceUploadResponse.model_validate(
            _json_fixture("trace-upload-success-false.json")
        )


def test_analyze_requires_non_empty_session_and_run_ids() -> None:
    response = SmartPerfettoAnalyzeResponse.model_validate(
        _json_fixture("analyze-success.json")
    )

    assert response.session_id == "session-synthetic-001"
    assert response.run_id == "run-session-synthetic-001-1"
    for missing_field in ("sessionId", "runId"):
        payload = _json_fixture("analyze-success.json")
        payload[missing_field] = ""
        with pytest.raises(ValidationError):
            SmartPerfettoAnalyzeResponse.model_validate(payload)


@pytest.mark.parametrize(
    "fixture_name,action",
    [
        ("analyze-smart-preview-request.json", "preview"),
        ("analyze-smart-deep-dive-request.json", "analyze"),
    ],
)
def test_smart_requests_serialize_exactly_with_upstream_native_options(
    fixture_name: str,
    action: str,
) -> None:
    fixture = _json_fixture(fixture_name)
    request = SmartPerfettoAnalyzeRequest.model_validate(fixture)

    assert request.model_dump(mode="json", by_alias=True, exclude_none=True) == fixture
    assert request.options.analysis_mode == "auto"
    assert request.options.preset == "smart"
    assert request.options.smart_action == action
    serialized = json.dumps(fixture, sort_keys=True)
    assert '"analysisMode": "startup"' not in serialized
    assert '"analysisMode": "scroll"' not in serialized
    assert "providerId" not in serialized
    assert "codebaseIds" not in serialized


def test_smart_deep_dive_selection_is_limited_to_reviewed_scene_types() -> None:
    fixture = _json_fixture("analyze-smart-deep-dive-request.json")
    request = SmartPerfettoAnalyzeRequest.model_validate(fixture)

    assert request.options.smart_selection is not None
    assert set(request.options.smart_selection.scene_types) == {
        "cold_start",
        "warm_start",
        "hot_start",
        "scroll",
        "inertial_scroll",
    }

    fixture["options"]["smartSelection"]["sceneTypes"].append("navigation")  # type: ignore[index]
    with pytest.raises(ValidationError):
        SmartPerfettoAnalyzeRequest.model_validate(fixture)


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "running",
        "awaiting_user",
        "completed",
        "failed",
        "cancelled",
        "quota_exceeded",
    ],
)
def test_status_accepts_only_the_seven_reviewed_spellings(status: str) -> None:
    payload = _json_fixture("status-completed.json")
    payload["status"] = status

    assert SmartPerfettoStatusResponse.model_validate(payload).status == status


def test_status_rejects_unknown_spelling() -> None:
    payload = _json_fixture("status-completed.json")
    payload["status"] = "canceled"

    with pytest.raises(ValidationError):
        SmartPerfettoStatusResponse.model_validate(payload)


def test_resume_does_not_require_trace_id_and_projects_observability_run_id() -> None:
    payload = _json_fixture("resume-success.json")
    payload.pop("traceId")

    response = SmartPerfettoResumeResponse.model_validate(payload)

    assert response.session_id == "session-synthetic-001"
    assert response.run_id == "run-session-synthetic-001-2-recovered"


def test_cancel_accepts_only_upstream_cancelled_spelling() -> None:
    response = SmartPerfettoCancelResponse.model_validate(
        _json_fixture("cancel-success.json")
    )

    assert response.status == "cancelled"
    with pytest.raises(ValidationError):
        SmartPerfettoCancelResponse.model_validate(
            {"success": True, "sessionId": "session-1", "status": "canceled"}
        )


def test_endpoint_errors_parse_machine_codes_without_consuming_human_text() -> None:
    concurrent = SmartPerfettoEndpointError.model_validate(
        _json_fixture("concurrent-quota.json")
    )
    monthly = SmartPerfettoEndpointError.model_validate(
        _json_fixture("monthly-quota.json")
    )

    assert concurrent.code == "CONCURRENT_RUN_QUOTA_EXCEEDED"
    assert monthly.code == "MONTHLY_RUN_QUOTA_EXCEEDED"
    assert "Synthetic" not in repr(concurrent)
    assert "Synthetic" not in repr(monthly)


def test_report_extracts_safe_id_and_recursively_sanitizes_retained_payload() -> None:
    fixture = _json_fixture("report-completed.json")
    response = SmartPerfettoReportResponse.model_validate(fixture)
    serialized = json.dumps(response.sanitized_report, sort_keys=True)

    assert response.report_id == "report-synthetic-001"
    assert response.sanitized_report["summary"]["conclusion"].startswith(  # type: ignore[index]
        "The synthetic startup interval"
    )
    assert "[redacted]" in serialized
    for unsafe in (
        "logFile",
        "authorization",
        "apiKey",
        "token",
        "objectKey",
        "bucket",
        "synthetic-secret",
        "X-Amz-Signature",
        "s3://",
        "/synthetic/private",
        "C:\\\\synthetic",
    ):
        assert unsafe.casefold() not in serialized.casefold()


def test_report_retains_only_the_reviewed_normalization_typed_fields() -> None:
    payload = _json_fixture("report-completed.json")
    report = payload["report"]
    assert isinstance(report, dict)
    report["dataEnvelopes"] = [{"type": "data-envelope@1", "id": "startup"}]
    report["diagnostics"] = [{"id": "startup.delay", "severity": "warning"}]
    report["actions"] = [{"id": "unapproved-action"}]
    report["unknownPrivateField"] = "must-not-survive"

    response = SmartPerfettoReportResponse.model_validate(payload)

    assert response.sanitized_report["dataEnvelopes"] == report["dataEnvelopes"]
    assert response.sanitized_report["diagnostics"] == report["diagnostics"]
    assert "actions" not in response.sanitized_report
    assert "unknownPrivateField" not in response.sanitized_report


def test_report_rejects_arbitrary_absolute_report_url() -> None:
    unsafe_url = "https://objects.invalid/report/private?token=must-not-leak"
    payload = {"success": True, "report": {"reportUrl": unsafe_url}}

    with pytest.raises(ValidationError) as exc_info:
        SmartPerfettoReportResponse.model_validate(payload)
    assert unsafe_url not in str(exc_info.value)
    assert unsafe_url not in repr(exc_info.value)


def test_smart_preview_requires_report_id_and_bounded_scene_objects() -> None:
    event = _sse_data_fixture("smart-preview-stream.sse", "analysis_completed")
    preview = SmartPerfettoScenePreview.model_validate(
        event["data"]["smartScenePreview"]  # type: ignore[index]
    )

    assert preview.report_id == "report-synthetic-preview-001"
    assert [(scene.id, scene.scene_type) for scene in preview.scenes] == [
        ("scene-startup-001", "cold_start"),
        ("scene-scroll-001", "scroll"),
        ("scene-ignored-001", "navigation"),
    ]

    missing_id = event["data"]["smartScenePreview"]  # type: ignore[index]
    missing_id.pop("reportId")
    with pytest.raises(ValidationError):
        SmartPerfettoScenePreview.model_validate(missing_id)

    with pytest.raises(ValidationError):
        SmartPerfettoScenePreview.model_validate(
            {
                "reportId": "report-too-many-scenes",
                "scenes": [
                    {"id": f"scene-{index}", "sceneType": "scroll"}
                    for index in range(129)
                ],
            }
        )


def test_unknown_extra_fields_are_ignored_but_required_fields_fail_closed() -> None:
    payload = _json_fixture("analyze-success.json")
    payload["futureUpstreamField"] = {"ignored": True}

    response = SmartPerfettoAnalyzeResponse.model_validate(payload)

    assert "futureUpstreamField" not in response.model_dump(by_alias=True)
    payload.pop("sessionId")
    with pytest.raises(ValidationError):
        SmartPerfettoAnalyzeResponse.model_validate(payload)


def test_validation_errors_never_echo_unsafe_consumer_input() -> None:
    unsafe_value = "https://objects.invalid/private?token=must-not-leak"

    with pytest.raises(ValidationError) as exc_info:
        SmartPerfettoAnalyzeResponse.model_validate(
            {
                "success": True,
                "sessionId": unsafe_value,
                "runId": "run-synthetic-001",
            }
        )
    assert unsafe_value not in str(exc_info.value)
    assert unsafe_value not in repr(exc_info.value)
    assert "must-not-leak" not in str(exc_info.value)
    assert "must-not-leak" not in repr(exc_info.value)
