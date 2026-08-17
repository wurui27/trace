"""Private consumer models for the pinned SmartPerfetto HTTP contract.

SmartPerfetto does not advertise or negotiate ``workspace-agent-v1``. The
name belongs to PerfPilot and identifies the narrow surface validated here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)


_OpaqueId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
_ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
]
_UpstreamStatus = Literal[
    "pending",
    "running",
    "awaiting_user",
    "completed",
    "failed",
    "cancelled",
    "quota_exceeded",
]
_SupportedSceneType = Literal[
    "cold_start",
    "warm_start",
    "hot_start",
    "scroll",
    "inertial_scroll",
]

_REPORT_URL = re.compile(r"^/api/reports/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})$")
_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SIGNED_HTTP_URL = re.compile(
    r"(?i)https?://[^\s]+[?&](?:x-amz-signature|x-goog-signature|signature|sig|token)="
)
_CREDENTIAL_MARKER = re.compile(
    r"(?i)(?:\bbearer\s+|\bauthorization\b|\bapi[ _-]?key\b|\baccess[ _-]?token\b)"
)
_OBJECT_STORE_URI = re.compile(r"(?i)^(?:s3|gs|az|r2)://")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_REPORT_KEYS = {
    "apikey",
    "authorization",
    "bucket",
    "logfile",
    "objectkey",
    "token",
}
_ALLOWED_REPORT_KEYS = {
    "sessionId",
    "traceId",
    "query",
    "createdAt",
    "completedAt",
    "summary",
    "reportUrl",
    "reportError",
    "resultSnapshotId",
    "claimSupport",
    "claimVerificationResult",
    "dataEnvelopes",
    "diagnostics",
    "identityResolutions",
    "findings",
    "hypotheses",
    "conversationTimeline",
    "queryHistory",
    "conclusionHistory",
    "analysisNotes",
    "analysisPlan",
    "uncertaintyFlags",
    "resultContract",
}
_ALLOWED_STABLE_REPORT_KEYS = (_ALLOWED_REPORT_KEYS - {"reportUrl", "reportError"}) | {
    "reportId"
}
_MAX_REPORT_DEPTH = 12
_MAX_REPORT_COLLECTION_ITEMS = 512
_MAX_REPORT_STRING_LENGTH = 16_384
# Keep the validated report within the same bound as one canonical engine
# envelope. Real SmartPerfetto result contracts can exceed 512 KiB while the
# bounded raw report and canonical persistence layers both allow 2 MiB.
_MAX_SANITIZED_REPORT_BYTES = 2 * 1024 * 1024


class _FrozenConsumerModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class SmartPerfettoWorkspace(_FrozenConsumerModel):
    id: _OpaqueId


class SmartPerfettoWorkspaceListResponse(_FrozenConsumerModel):
    success: Literal[True]
    workspaces: tuple[SmartPerfettoWorkspace, ...] = Field(max_length=512)


class SmartPerfettoWorkspaceCreateResponse(_FrozenConsumerModel):
    success: Literal[True]
    workspace: SmartPerfettoWorkspace


class SmartPerfettoTrace(_FrozenConsumerModel):
    id: _OpaqueId


class SmartPerfettoTraceUploadResponse(_FrozenConsumerModel):
    success: Literal[True]
    trace: SmartPerfettoTrace


class SmartPerfettoSceneSelection(_FrozenConsumerModel):
    scope: Literal["scene_types"]
    scene_types: tuple[_SupportedSceneType, ...] = Field(
        alias="sceneTypes",
        min_length=1,
        max_length=5,
    )
    label: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    report_id: _OpaqueId = Field(alias="reportId")

    @field_validator("scene_types")
    @classmethod
    def _require_unique_scene_types(
        cls,
        value: tuple[_SupportedSceneType, ...],
    ) -> tuple[_SupportedSceneType, ...]:
        if len(set(value)) != len(value):
            raise ValueError("scene types must be unique")
        return value


class SmartPerfettoAnalyzeOptions(_FrozenConsumerModel):
    analysis_mode: Literal["auto"] = Field(alias="analysisMode")
    preset: Literal["smart"]
    smart_action: Literal["preview", "analyze"] = Field(alias="smartAction")
    smart_selection: SmartPerfettoSceneSelection | None = Field(
        default=None,
        alias="smartSelection",
    )

    @model_validator(mode="after")
    def _selection_matches_action(self) -> SmartPerfettoAnalyzeOptions:
        if self.smart_action == "preview" and self.smart_selection is not None:
            raise ValueError("preview cannot include a scene selection")
        if self.smart_action == "analyze" and self.smart_selection is None:
            raise ValueError("analyze requires a scene selection")
        return self


class SmartPerfettoAnalyzeRequest(_FrozenConsumerModel):
    trace_id: _OpaqueId = Field(alias="traceId")
    query: _ShortText
    options: SmartPerfettoAnalyzeOptions


class SmartPerfettoAnalyzeResponse(_FrozenConsumerModel):
    success: Literal[True]
    session_id: _OpaqueId = Field(alias="sessionId")
    run_id: _OpaqueId = Field(alias="runId")


class SmartPerfettoObservability(_FrozenConsumerModel):
    run_id: _OpaqueId = Field(alias="runId")


class SmartPerfettoStatusResponse(_FrozenConsumerModel):
    success: Literal[True]
    session_id: _OpaqueId = Field(alias="sessionId")
    status: _UpstreamStatus


class SmartPerfettoResumeResponse(_FrozenConsumerModel):
    success: Literal[True]
    session_id: _OpaqueId = Field(alias="sessionId")
    status: _UpstreamStatus
    restored: bool
    observability: SmartPerfettoObservability | None = None

    @property
    def run_id(self) -> str | None:
        return self.observability.run_id if self.observability is not None else None


class SmartPerfettoCancelResponse(_FrozenConsumerModel):
    success: Literal[True]
    session_id: _OpaqueId = Field(alias="sessionId")
    status: Literal["cancelled"]


class SmartPerfettoEndpointError(_FrozenConsumerModel):
    success: Literal[False]
    code: _OpaqueId


class SmartPerfettoPreviewScene(_FrozenConsumerModel):
    id: _OpaqueId
    scene_type: _OpaqueId = Field(alias="sceneType")


class SmartPerfettoScenePreview(_FrozenConsumerModel):
    report_id: _OpaqueId = Field(alias="reportId")
    scenes: tuple[SmartPerfettoPreviewScene, ...] = Field(max_length=128)


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _redact_string(value: str) -> str:
    if len(value) > _MAX_REPORT_STRING_LENGTH:
        raise ValueError("report contract invalid")
    if (
        _CREDENTIAL_MARKER.search(value)
        or _SIGNED_HTTP_URL.search(value)
        or _OBJECT_STORE_URI.search(value)
        or value.startswith("/")
        or _WINDOWS_ABSOLUTE_PATH.search(value)
    ):
        return "[redacted]"
    return value


def _sanitize_nested(value: object, *, depth: int) -> object:
    if depth > _MAX_REPORT_DEPTH:
        raise ValueError("report contract invalid")
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_REPORT_COLLECTION_ITEMS:
            raise ValueError("report contract invalid")
        sanitized: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                continue
            if _normalized_key(key) in _FORBIDDEN_REPORT_KEYS:
                continue
            sanitized[key] = _sanitize_nested(nested, depth=depth + 1)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        if len(value) > _MAX_REPORT_COLLECTION_ITEMS:
            raise ValueError("report contract invalid")
        return [_sanitize_nested(nested, depth=depth + 1) for nested in value]
    raise ValueError("report contract invalid")


def _sanitize_report(report: Mapping[str, object]) -> tuple[dict[str, object], str]:
    report_url = report.get("reportUrl")
    if not isinstance(report_url, str):
        raise ValueError("report contract invalid")
    report_match = _REPORT_URL.fullmatch(report_url)
    if report_match is None:
        raise ValueError("report contract invalid")
    report_id = report_match.group(1)

    projection = {
        key: value
        for key, value in report.items()
        if key in _ALLOWED_REPORT_KEYS and key != "reportUrl"
    }
    projection["reportId"] = report_id
    sanitized = _sanitize_nested(projection, depth=0)
    if not isinstance(sanitized, dict):
        raise ValueError("report contract invalid")
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(serialized) > _MAX_SANITIZED_REPORT_BYTES:
        raise ValueError("report contract invalid")
    return sanitized, report_id


def validate_sanitized_report_payload(payload: object) -> dict[str, object]:
    """Validate that a stable report payload is a sanitizer fixed point."""

    if not isinstance(payload, Mapping) or set(payload) != {"reportId", "report"}:
        raise ValueError("report contract invalid")
    report_id = payload.get("reportId")
    report = payload.get("report")
    if (
        not isinstance(report_id, str)
        or len(report_id) > 255
        or _OPAQUE_ID_PATTERN.fullmatch(report_id) is None
        or not isinstance(report, Mapping)
        or "reportError" in report
        or not set(report).issubset(_ALLOWED_STABLE_REPORT_KEYS)
    ):
        raise ValueError("report contract invalid")
    if report.get("reportId") != report_id:
        raise ValueError("report contract invalid")
    sanitized = _sanitize_nested(report, depth=0)
    if not isinstance(sanitized, dict) or sanitized != report:
        raise ValueError("report contract invalid")
    try:
        encoded = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("report contract invalid") from None
    if len(encoded) > _MAX_SANITIZED_REPORT_BYTES:
        raise ValueError("report contract invalid")
    return {"reportId": report_id, "report": sanitized}


class SmartPerfettoReportResponse(_FrozenConsumerModel):
    success: Literal[True]
    sanitized_report: dict[str, object] = Field(alias="report")
    report_id: _OpaqueId = Field(alias="reportId")

    @model_validator(mode="before")
    @classmethod
    def _project_report(
        cls,
        value: object,
        _info: ValidationInfo,
    ) -> object:
        if not isinstance(value, Mapping) or value.get("success") is not True:
            return value
        report = value.get("report")
        if not isinstance(report, Mapping):
            raise ValueError("report contract invalid")
        sanitized, report_id = _sanitize_report(report)
        return {
            "success": True,
            "report": sanitized,
            "reportId": report_id,
        }


__all__ = [
    "SmartPerfettoAnalyzeRequest",
    "SmartPerfettoAnalyzeResponse",
    "SmartPerfettoCancelResponse",
    "SmartPerfettoEndpointError",
    "SmartPerfettoReportResponse",
    "SmartPerfettoResumeResponse",
    "SmartPerfettoScenePreview",
    "SmartPerfettoStatusResponse",
    "SmartPerfettoTraceUploadResponse",
    "SmartPerfettoWorkspaceCreateResponse",
    "SmartPerfettoWorkspaceListResponse",
    "validate_sanitized_report_payload",
]
