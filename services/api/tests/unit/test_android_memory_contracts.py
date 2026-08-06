from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from perfpilot_api.engines.android_memory_contracts import (
    AndroidMemoryContext,
    MemoryCaptureManifest,
)


def valid_manifest_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "analysis_id": "e2000000-0000-4000-8000-000000000001",
        "capture_id": "e3000000-0000-4000-8000-000000000001",
        "phase": "before",
        "source": "manual_upload",
        "captured_at": None,
        "subject": {"package": "com.example.app", "android_sdk": 37},
        "artifacts": [
            {
                "artifact_id": "e4000000-0000-4000-8000-000000000001",
                "role": "meminfo",
            }
        ],
    }


def valid_context_payload() -> dict[str, object]:
    return {
        "context_type": "android-memory-ai-context",
        "schema_version": "1.2",
        "generator": {"name": "android-memory-ai", "version": "1.2.0"},
        "analysis_contract": {
            "support_level": "limited",
            "primary_intent_support_level": "limited",
            "privacy": {
                "raw_contents_embedded": False,
                "local_paths_included": False,
            },
        },
    }


def test_capture_manifest_has_stable_canonical_bytes_and_hash() -> None:
    manifest = MemoryCaptureManifest.model_validate(valid_manifest_payload())

    expected = (
        b'{"analysis_id":"e2000000-0000-4000-8000-000000000001",'
        b'"artifacts":[{"artifact_id":"e4000000-0000-4000-8000-000000000001",'
        b'"role":"meminfo"}],"capture_id":"e3000000-0000-4000-8000-000000000001",'
        b'"phase":"before","schema_version":"1.0","source":"manual_upload",'
        b'"subject":{"android_sdk":37,"package":"com.example.app"}}'
    )

    assert manifest.canonical_bytes() == expected
    assert manifest.sha256_hex() == sha256(expected).hexdigest()


def test_capture_manifest_rejects_duplicate_artifact_ids() -> None:
    payload = valid_manifest_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.append({"artifact_id": artifacts[0]["artifact_id"], "role": "android_log"})

    with pytest.raises(ValidationError, match="artifact IDs must be unique"):
        MemoryCaptureManifest.model_validate(payload)


def test_capture_manifest_rejects_duplicate_singleton_roles() -> None:
    payload = valid_manifest_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.append({"artifact_id": "e4000000-0000-4000-8000-000000000002", "role": "meminfo"})

    with pytest.raises(ValidationError, match="singleton artifact roles"):
        MemoryCaptureManifest.model_validate(payload)


def test_capture_manifest_accepts_one_agent_handoff_archive() -> None:
    payload = valid_manifest_payload()
    payload["source"] = "adb_agent"
    payload["artifacts"] = [
        {
            "artifact_id": "e4000000-0000-4000-8000-000000000001",
            "role": "handoff_archive",
        }
    ]

    manifest = MemoryCaptureManifest.model_validate(payload)

    assert manifest.source == "adb_agent"
    assert manifest.artifacts[0].role == "handoff_archive"


def test_capture_manifest_rejects_multiple_agent_handoff_archives() -> None:
    payload = valid_manifest_payload()
    payload["source"] = "adb_agent"
    payload["artifacts"] = [
        {
            "artifact_id": "e4000000-0000-4000-8000-000000000001",
            "role": "handoff_archive",
        },
        {
            "artifact_id": "e4000000-0000-4000-8000-000000000002",
            "role": "handoff_archive",
        },
    ]

    with pytest.raises(ValidationError, match="singleton artifact roles"):
        MemoryCaptureManifest.model_validate(payload)


@pytest.mark.parametrize("role", ["auto", "android_log", "qa_screenshot", "previous_ai_context"])
def test_capture_manifest_allows_repeated_non_singleton_roles(role: str) -> None:
    payload = valid_manifest_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.extend(
        [
            {"artifact_id": "e4000000-0000-4000-8000-000000000002", "role": role},
            {"artifact_id": "e4000000-0000-4000-8000-000000000003", "role": role},
        ]
    )

    assert len(MemoryCaptureManifest.model_validate(payload).artifacts) == 3


@pytest.mark.parametrize(
    "captured_at",
    [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))],
)
def test_capture_manifest_rejects_naive_and_non_utc_capture_times(captured_at: datetime) -> None:
    payload = valid_manifest_payload()
    payload["captured_at"] = captured_at

    with pytest.raises(ValidationError, match="UTC"):
        MemoryCaptureManifest.model_validate(payload)


def test_capture_manifest_serializes_utc_timestamp_normally() -> None:
    payload = valid_manifest_payload()
    payload["captured_at"] = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    assert b'"captured_at":"2026-01-02T03:04:05Z"' in (
        MemoryCaptureManifest.model_validate(payload).canonical_bytes()
    )


def test_capture_manifest_rejects_more_than_2048_artifacts() -> None:
    payload = valid_manifest_payload()
    payload["artifacts"] = [
        {"artifact_id": str(UUID(int=index + 1)), "role": "auto"} for index in range(2049)
    ]

    with pytest.raises(ValidationError):
        MemoryCaptureManifest.model_validate(payload)


@pytest.mark.parametrize(
    "location_key",
    ["team_id", "file_name", "object_key", "url", "sha256", "size", "local_path"],
)
def test_capture_manifest_forbids_storage_and_privacy_fields(location_key: str) -> None:
    payload = valid_manifest_payload()
    payload[location_key] = "not-permitted"

    with pytest.raises(ValidationError):
        MemoryCaptureManifest.model_validate(payload)


@pytest.mark.parametrize(
    "subject",
    [
        {"package": "com..example"},
        {"package": "com.example.app", "pid": 0},
        {"package": "com.example.app", "android_release": "x" * 65},
        {"package": "com.example.app", "android_sdk": 0},
        {"package": "com.example.app", "android_sdk": 101},
    ],
)
def test_capture_manifest_rejects_invalid_subject_bounds(subject: dict[str, object]) -> None:
    payload = valid_manifest_payload()
    payload["subject"] = subject

    with pytest.raises(ValidationError):
        MemoryCaptureManifest.model_validate(payload)


def test_capture_manifest_accepts_uppercase_android_package_segments() -> None:
    payload = valid_manifest_payload()
    payload["subject"] = {"package": "com.Example.App_2"}

    assert MemoryCaptureManifest.model_validate(payload).subject.package == "com.Example.App_2"


@pytest.mark.parametrize(
    "package",
    ["1com.example", "com.ex-ample.app", "com..example", "comexample"],
)
def test_capture_manifest_rejects_invalid_android_package_segments(package: str) -> None:
    payload = valid_manifest_payload()
    payload["subject"] = {"package": package}

    with pytest.raises(ValidationError):
        MemoryCaptureManifest.model_validate(payload)


@pytest.mark.parametrize("captured_at", [0, 0.0, b"2026-01-02T03:04:05Z", False, []])
def test_capture_manifest_rejects_non_text_and_non_datetime_capture_times(
    captured_at: object,
) -> None:
    payload = valid_manifest_payload()
    payload["captured_at"] = captured_at

    with pytest.raises(ValidationError):
        MemoryCaptureManifest.model_validate(payload)


@pytest.mark.parametrize(
    "captured_at",
    ["2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)],
)
def test_capture_manifest_accepts_utc_text_and_datetime_capture_times(captured_at: object) -> None:
    payload = valid_manifest_payload()
    payload["captured_at"] = captured_at

    assert b'"captured_at":"2026-01-02T03:04:05Z"' in (
        MemoryCaptureManifest.model_validate(payload).canonical_bytes()
    )


def test_capture_manifest_and_nested_models_are_immutable() -> None:
    manifest = MemoryCaptureManifest.model_validate(valid_manifest_payload())

    with pytest.raises(ValidationError):
        manifest.phase = "after"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        manifest.subject.package = "com.other.app"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        manifest.artifacts[0].role = "smaps"  # type: ignore[misc]


def test_upstream_context_preserves_unknown_fields_at_each_required_level() -> None:
    payload = valid_context_payload()
    payload["upstream_extension"] = {"preserved": True}
    generator = payload["generator"]
    assert isinstance(generator, dict)
    generator["build"] = "pinned-build"
    analysis_contract = payload["analysis_contract"]
    assert isinstance(analysis_contract, dict)
    analysis_contract["new_contract_property"] = ["retained"]
    privacy = analysis_contract["privacy"]
    assert isinstance(privacy, dict)
    privacy["new_privacy_property"] = "retained"

    context = AndroidMemoryContext.model_validate(payload)

    assert context.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("context_type",), "wrong"),
        (("schema_version",), "1.3"),
        (("generator", "name"), "other"),
        (("generator", "version"), "1.2.1"),
    ],
)
def test_upstream_context_rejects_wrong_required_values(path: tuple[str, ...], value: str) -> None:
    payload = valid_context_payload()
    target: dict[str, object] = payload
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        AndroidMemoryContext.model_validate(payload)


@pytest.mark.parametrize("invalid_value", [0, "false", None, True])
def test_upstream_context_rejects_non_false_local_paths(invalid_value: object) -> None:
    payload = valid_context_payload()
    payload["analysis_contract"]["privacy"]["local_paths_included"] = invalid_value
    with pytest.raises(ValidationError):
        AndroidMemoryContext.model_validate(payload)


@pytest.mark.parametrize("invalid_value", [0, "false", None, True])
def test_upstream_context_rejects_non_false_raw_contents(invalid_value: object) -> None:
    payload = valid_context_payload()
    payload["analysis_contract"]["privacy"]["raw_contents_embedded"] = invalid_value
    with pytest.raises(ValidationError):
        AndroidMemoryContext.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("support_level", "Insufficient"),
        ("support_level", "insufficient_data"),
        ("support_level", "unknown"),
        ("support_level", 1),
        ("primary_intent_support_level", "Insufficient"),
        ("primary_intent_support_level", "insufficient_data"),
        ("primary_intent_support_level", "unknown"),
        ("primary_intent_support_level", 1),
    ],
)
def test_upstream_context_rejects_non_exact_support_levels(
    field: str, invalid_value: object
) -> None:
    payload = valid_context_payload()
    payload["analysis_contract"][field] = invalid_value

    with pytest.raises(ValidationError):
        AndroidMemoryContext.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "path",
    [
        ("analysis_contract", "support_level"),
        ("analysis_contract", "primary_intent_support_level"),
        ("analysis_contract", "privacy", "raw_contents_embedded"),
        ("analysis_contract", "privacy", "local_paths_included"),
    ],
)
def test_upstream_context_requires_every_support_and_privacy_field(
    path: tuple[str, ...],
) -> None:
    payload = valid_context_payload()
    target: dict[str, object] = payload
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    del target[path[-1]]

    with pytest.raises(ValidationError):
        AndroidMemoryContext.model_validate(payload, strict=True)


def test_contract_errors_redact_rejected_input_values() -> None:
    marker = "unique-secret-marker-for-redaction"
    payload = valid_context_payload()
    payload["context_type"] = marker

    with pytest.raises(ValidationError) as raised:
        AndroidMemoryContext.model_validate(payload)

    assert marker not in str(raised.value)
    assert marker not in repr(raised.value)
