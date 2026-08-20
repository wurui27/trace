from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping
from uuid import UUID

from perfpilot_api.local_analysis_store import migrate_analysis_runtime_status


_CORE_PERSISTED_KEYS = frozenset(
    {
        "schema_version",
        "team_id",
        "analysis_id",
        "state",
        "version",
        "generation",
        "created_at",
        "started_at",
        "completed_at",
        "cancel_requested_at",
        "report_available",
        "response_schema_version",
        "runtime_status",
    }
)
_PAYLOAD_PERSISTED_KEYS = frozenset(
    {
        "analysis_mode",
        "device_id",
        "device_agent_id",
        "device_digest",
        "application_version_id",
        "application_metadata",
        "capture_configuration",
        "trace_test_type",
        "target_package_name",
        "custom_test_name",
        "custom_test_description",
        "remote_publication",
        "profile",
        "question",
        "inputs",
        "failure",
        "stages",
        "source_run",
        "source_rounds",
        "source_verification",
        "source_binding",
        "source_code_analysis",
        "ai_rounds",
        "evidence_format_version",
        "evidence_manifest",
        "smartperfetto_original",
    }
)
_PERSISTED_KEYS = _CORE_PERSISTED_KEYS | _PAYLOAD_PERSISTED_KEYS
_REQUIRED_PERSISTED_KEYS = frozenset(
    {
        "schema_version",
        "team_id",
        "analysis_id",
        "analysis_mode",
        "profile",
        "question",
        "state",
        "version",
        "generation",
        "inputs",
        "failure",
        "stages",
        "report_available",
    }
)
_PUBLIC_COMMON_KEYS = frozenset(
    {
        "schema_version",
        "analysis_id",
        "team_id",
        "analysis_mode",
        "state",
        "version",
        "created_at",
        "cancel_requested_at",
        "report_available",
        "failure",
        "source_code_analysis",
        "runtime_status",
    }
)
_PUBLIC_TRACE_KEYS = frozenset(
    {
        "analysis_profile",
        "test_type",
        "package_name",
        "custom_test_name",
        "custom_test_description",
        "question",
        "input_uploads",
        "stages",
        "ai_rounds",
        "source_analysis",
    }
)
_PUBLIC_DEVICE_KEYS = frozenset(
    {
        "device_id",
        "application_version_id",
        "application_metadata",
        "capture_configuration",
        "apk_upload",
        "scenarios",
        "sample_verdict_counts",
        "active_lease",
        "started_at",
        "completed_at",
    }
)
_PUBLIC_MEMORY_KEYS = frozenset(
    {"application_version_id", "application_metadata", "question"}
)
_PRIVATE_SOURCE_KEYS = frozenset(
    {"relative_path", "symbol", "diff", "content", "private_path"}
)
_TERMINAL_STATES = frozenset(
    {"completed", "partially_completed", "failed", "canceled", "deleted"}
)


class LocalAnalysisProjectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalAnalysisView:
    analysis_id: UUID
    team_id: UUID
    schema_version: str
    state: str
    version: int
    generation: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    report_available: bool
    runtime_status: Mapping[str, object]
    payload: Mapping[str, object]


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LocalAnalysisProjectionError("persisted analysis rejected") from None
    if parsed.tzinfo is None:
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    return parsed.astimezone(UTC)


def _validate_payload_keys(payload: Mapping[str, object]) -> None:
    keys = set(payload) - {"public_document"}
    if not keys.issubset(_PAYLOAD_PERSISTED_KEYS):
        raise LocalAnalysisProjectionError("persisted analysis rejected")


def to_persisted_document(value: LocalAnalysisView) -> dict[str, object]:
    _validate_payload_keys(value.payload)
    payload = {
        key: item
        for key, item in value.payload.items()
        if key in _PAYLOAD_PERSISTED_KEYS
    }
    document: dict[str, object] = {
        "schema_version": "1.0",
        **payload,
        "team_id": str(value.team_id),
        "analysis_id": str(value.analysis_id),
        "created_at": _timestamp(value.created_at),
        "started_at": _timestamp(value.started_at),
        "completed_at": _timestamp(value.completed_at),
        "state": value.state,
        "version": value.version,
        "generation": value.generation,
        "response_schema_version": value.schema_version,
        "runtime_status": dict(value.runtime_status),
        "cancel_requested_at": _timestamp(value.cancel_requested_at),
        "report_available": value.report_available,
    }
    if set(document) - _PERSISTED_KEYS or not _REQUIRED_PERSISTED_KEYS.issubset(
        document
    ):
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    return document


def from_persisted_document(document: Mapping[str, object]) -> LocalAnalysisView:
    if (
        set(document) - _PERSISTED_KEYS
        or not _REQUIRED_PERSISTED_KEYS.issubset(document)
        or document.get("schema_version") != "1.0"
        or type(document.get("version")) is not int
        or int(document["version"]) < 1
        or type(document.get("generation")) is not int
        or int(document["generation"]) < 1
        or not isinstance(document.get("state"), str)
        or not isinstance(document.get("analysis_mode"), str)
        or not isinstance(document.get("stages"), Mapping)
        or type(document.get("report_available")) is not bool
    ):
        raise LocalAnalysisProjectionError("persisted analysis rejected")
    try:
        team_id = UUID(str(document["team_id"]))
        analysis_id = UUID(str(document["analysis_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise LocalAnalysisProjectionError("persisted analysis rejected") from None
    created_at = _parse_timestamp(document.get("created_at"))
    if created_at is None:
        created_at = datetime(1970, 1, 1, tzinfo=UTC)
    started_at = _parse_timestamp(document.get("started_at"))
    completed_at = _parse_timestamp(document.get("completed_at"))
    cancel_requested_at = _parse_timestamp(document.get("cancel_requested_at"))
    generation = int(document["generation"])
    updated_at = (completed_at or started_at or created_at).isoformat()
    try:
        runtime_status = migrate_analysis_runtime_status(
            document.get("runtime_status"),
            state=str(document["state"]),
            generation=generation,
            updated_at=updated_at,
            stages=document["stages"],
        )
    except Exception:
        raise LocalAnalysisProjectionError("persisted analysis rejected") from None
    payload = {
        key: item for key, item in document.items() if key in _PAYLOAD_PERSISTED_KEYS
    }
    return LocalAnalysisView(
        analysis_id=analysis_id,
        team_id=team_id,
        schema_version=str(document.get("response_schema_version") or "1.0"),
        state=str(document["state"]),
        version=int(document["version"]),
        generation=generation,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        cancel_requested_at=cancel_requested_at,
        report_available=bool(document["report_available"]),
        runtime_status=runtime_status,
        payload=payload,
    )


def _contains_private_source_key(value: object) -> bool:
    if isinstance(value, Mapping):
        if _PRIVATE_SOURCE_KEYS.intersection(value):
            return True
        return any(_contains_private_source_key(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_private_source_key(item) for item in value)
    return False


def to_public_document(value: LocalAnalysisView) -> dict[str, object]:
    raw = value.payload.get("public_document")
    if not isinstance(raw, Mapping):
        raise LocalAnalysisProjectionError("public analysis rejected")
    document = dict(raw)
    mode = document.get("analysis_mode")
    allowed = _PUBLIC_COMMON_KEYS | (
        _PUBLIC_TRACE_KEYS
        if mode == "trace_upload"
        else _PUBLIC_DEVICE_KEYS
        if mode == "device"
        else _PUBLIC_MEMORY_KEYS
        if mode == "memory_upload"
        else frozenset()
    )
    if (
        not allowed
        or set(document) - allowed
        or document.get("analysis_id") != str(value.analysis_id)
        or document.get("team_id") != str(value.team_id)
        or document.get("schema_version") != value.schema_version
        or document.get("state") != value.state
        or document.get("version") != value.version
        or document.get("report_available") is not value.report_available
    ):
        raise LocalAnalysisProjectionError("public analysis rejected")
    has_runtime = "runtime_status" in document
    if (value.schema_version == "1.3") != has_runtime:
        raise LocalAnalysisProjectionError("public analysis rejected")
    if has_runtime and document["runtime_status"] != dict(value.runtime_status):
        raise LocalAnalysisProjectionError("public analysis rejected")
    source = document.get("source_code_analysis")
    if isinstance(source, Mapping) and source.get("match_summary") != "strong":
        if _contains_private_source_key(source):
            raise LocalAnalysisProjectionError("public analysis rejected")
    if value.state in _TERMINAL_STATES and has_runtime:
        actions = value.runtime_status.get("available_actions")
        if actions != []:
            raise LocalAnalysisProjectionError("public analysis rejected")
    return document


__all__ = [
    "LocalAnalysisProjectionError",
    "LocalAnalysisView",
    "from_persisted_document",
    "to_persisted_document",
    "to_public_document",
]
