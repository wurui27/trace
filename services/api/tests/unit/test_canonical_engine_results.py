from __future__ import annotations

import base64
import hashlib
import inspect
import json
from dataclasses import FrozenInstanceError, replace
from typing import Any
from urllib.parse import quote
from uuid import UUID

import pytest

from perfpilot_api.engines.canonical_results import (
    CanonicalEngineResult,
    EngineResultValidationError,
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.engines.smartperfetto_contracts import (
    validate_sanitized_report_payload,
)


TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("00000000-0000-4000-8000-000000000001")
SMART_EXECUTION_ID = UUID("00000000-0000-4000-8000-000000000101")
ANDROID_EXECUTION_ID = UUID("00000000-0000-4000-8000-000000000102")
SMART_ARTIFACT_ID = UUID("c79e45ad-fcb4-5b16-a327-f3aae70eebbc")
ANDROID_ARTIFACT_ID = UUID("5f9cc3e2-5d41-5db9-8964-38dc5cd819b4")


def _smart_payload(*, conclusion: str = "Main thread blocked") -> dict[str, object]:
    return {
        "reportId": "report-1",
        "report": {
            "reportId": "report-1",
            "summary": {"conclusion": conclusion},
        },
    }


def _android_payload(**extension: object) -> dict[str, object]:
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
        **extension,
    }


def _smart_write(
    *,
    payload: dict[str, object] | None = None,
    state: str = "completed",
    contract: str = "workspace-agent-v1",
    **changes: object,
) -> EngineResultWrite:
    values: dict[str, object] = {
        "team_id": TEAM_ID,
        "analysis_id": ANALYSIS_ID,
        "execution_id": SMART_EXECUTION_ID,
        "expected_execution_version": 3,
        "tenant_resource_version": 7,
        "artifact_id": SMART_ARTIFACT_ID,
        "engine_id": "smartperfetto",
        "adapter_version": "1.0.0",
        "engine_commit_sha": "1" * 40,
        "engine_image_digest": f"sha256:{'1' * 64}",
        "attempt_number": 1,
        "input_manifest_hash": "2" * 64,
        "config_hash": "3" * 64,
        "result": EngineResult(
            contract=contract,
            state=state,  # type: ignore[arg-type]
            payload=_smart_payload() if payload is None else payload,
        ),
    }
    values.update(changes)
    return EngineResultWrite(**values)  # type: ignore[arg-type]


def _android_write(
    *,
    payload: dict[str, object] | None = None,
    state: str = "completed",
    contract: str = "android-memory-ai-context-1.2",
    **changes: object,
) -> EngineResultWrite:
    values: dict[str, object] = {
        "team_id": TEAM_ID,
        "analysis_id": UUID("00000000-0000-4000-8000-000000000002"),
        "execution_id": ANDROID_EXECUTION_ID,
        "expected_execution_version": 2,
        "tenant_resource_version": 1,
        "artifact_id": ANDROID_ARTIFACT_ID,
        "engine_id": "android_memory",
        "adapter_version": "1.0.0",
        "engine_commit_sha": "d5514972ced78c3faa7fc17589c1ea9231645056",
        "engine_image_digest": f"sha256:{'4' * 64}",
        "attempt_number": 1,
        "input_manifest_hash": "5" * 64,
        "config_hash": "6" * 64,
        "result": EngineResult(
            contract=contract,
            state=state,  # type: ignore[arg-type]
            payload=_android_payload() if payload is None else payload,
        ),
    }
    values.update(changes)
    return EngineResultWrite(**values)  # type: ignore[arg-type]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _assert_invalid(request: EngineResultWrite, marker: str | None = None) -> None:
    with pytest.raises(EngineResultValidationError) as raised:
        canonicalize_engine_result(request)

    assert str(raised.value) == "engine result is invalid"
    assert repr(raised.value) == "EngineResultValidationError('engine result is invalid')"
    if marker is not None:
        assert marker not in str(raised.value)
        assert marker not in repr(raised.value)


def test_result_artifact_identity_is_stable_and_namespaced_by_execution() -> None:
    assert result_artifact_id(SMART_EXECUTION_ID) == SMART_ARTIFACT_ID
    assert result_artifact_id(ANDROID_EXECUTION_ID) == ANDROID_ARTIFACT_ID
    assert result_artifact_id(SMART_EXECUTION_ID) != result_artifact_id(
        UUID("00000000-0000-4000-8000-000000000103")
    )


def test_value_objects_are_frozen_and_hide_sensitive_mutable_values_from_repr() -> None:
    marker = "private-payload-marker"
    request = _smart_write(payload=_smart_payload(conclusion=marker))
    canonical = canonicalize_engine_result(request)

    assert marker not in repr(request)
    assert marker not in repr(canonical)
    assert repr(canonical.canonical_bytes) not in repr(canonical)
    assert canonical.checksum_sha256_b64 not in repr(canonical)
    assert isinstance(canonical, CanonicalEngineResult)
    with pytest.raises(FrozenInstanceError):
        request.attempt_number = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        canonical.request_hash_hex = "0" * 64  # type: ignore[misc]


def test_insertion_order_produces_identical_canonical_bytes_and_hashes() -> None:
    first_payload = _smart_payload()
    second_payload = {
        "report": {
            "summary": {"conclusion": "Main thread blocked"},
            "reportId": "report-1",
        },
        "reportId": "report-1",
    }

    first = canonicalize_engine_result(_smart_write(payload=first_payload))
    second = canonicalize_engine_result(_smart_write(payload=second_payload))

    assert first.canonical_bytes == second.canonical_bytes
    assert first.payload_sha256_hex == second.payload_sha256_hex
    assert first.request_hash_hex == second.request_hash_hex
    assert first.checksum_sha256_b64 == second.checksum_sha256_b64


def test_non_ascii_text_remains_utf8_and_hashes_cover_the_exact_boundaries() -> None:
    payload = _smart_payload(conclusion="主线程阻塞 — café")
    canonical = canonicalize_engine_result(_smart_write(payload=payload))
    expected_payload_bytes = _canonical_json(payload)

    assert "主线程阻塞".encode() in canonical.canonical_bytes
    assert b"\\u4e3b" not in canonical.canonical_bytes
    assert canonical.payload_sha256_hex == hashlib.sha256(
        expected_payload_bytes
    ).hexdigest()
    assert canonical.request_hash_hex == hashlib.sha256(
        canonical.canonical_bytes
    ).hexdigest()
    assert canonical.checksum_sha256_b64 == base64.b64encode(
        hashlib.sha256(canonical.canonical_bytes).digest()
    ).decode("ascii")
    assert canonical.document["result"] == {
        "state": "completed",
        "payload_sha256": canonical.payload_sha256_hex,
        "payload": payload,
    }


@pytest.mark.parametrize(
    ("write", "expected_hash"),
    [
        (
            _smart_write(),
            "07b9aa68bf4d16936ee0d6f7b0234db1cc1d2e1e6b80e7c0a6fa8963289723b3",
        ),
        (
            _android_write(),
            "3960e8b9c6487d4c1a61d45fcbad511c048d721ff76047dac45ba89802eb7907",
        ),
    ],
)
def test_payload_hash_matches_the_byte_accurate_contract_example(
    write: EngineResultWrite,
    expected_hash: str,
) -> None:
    assert canonicalize_engine_result(write).payload_sha256_hex == expected_hash


def test_canonicalization_defensively_copies_before_returning() -> None:
    payload = _smart_payload()
    canonical = canonicalize_engine_result(_smart_write(payload=payload))
    original_bytes = canonical.canonical_bytes
    report = payload["report"]
    assert isinstance(report, dict)
    summary = report["summary"]
    assert isinstance(summary, dict)

    summary["conclusion"] = "mutated after canonicalization"
    payload["reportId"] = "mutated-id"

    assert canonical.canonical_bytes == original_bytes
    parsed = json.loads(canonical.canonical_bytes)
    assert parsed["result"]["payload"]["reportId"] == "report-1"
    assert canonical.document["result"] != {"payload": payload}


def test_envelope_contains_only_the_closed_public_provenance() -> None:
    canonical = canonicalize_engine_result(_smart_write())
    encoded = canonical.canonical_bytes.decode("utf-8")
    document = json.loads(encoded)

    assert set(document) == {
        "schema_version",
        "result_type",
        "artifact_id",
        "analysis_id",
        "execution_id",
        "tenant_resource_version",
        "engine",
        "attempt",
        "result",
    }
    for forbidden in (
        "team_id",
        "expected_execution_version",
        "created_at",
        "completed_at",
        "bucket",
        "object_key",
        "version_id",
        "signed_url",
    ):
        assert forbidden not in encoded.casefold()


def test_canonicalization_is_a_synchronous_pure_boundary() -> None:
    assert not inspect.iscoroutinefunction(canonicalize_engine_result)
    assert "await" not in inspect.getsource(canonicalize_engine_result)


@pytest.mark.parametrize("state", ["failed", "canceled", "running", "pending"])
def test_non_result_terminal_states_are_rejected(state: str) -> None:
    _assert_invalid(_smart_write(state=state))


@pytest.mark.parametrize("write", [_smart_write(), _android_write()])
def test_insufficient_data_is_a_valid_canonical_result_state(
    write: EngineResultWrite,
) -> None:
    request = replace(write, result=replace(write.result, state="insufficient_data"))

    canonical = canonicalize_engine_result(request)

    result = canonical.document["result"]
    assert isinstance(result, dict)
    assert result["state"] == "insufficient_data"


@pytest.mark.parametrize(
    ("write", "contract"),
    [
        (_smart_write(), "android-memory-ai-context-1.2"),
        (_android_write(), "workspace-agent-v1"),
        (_smart_write(), "workspace-agent-v2"),
    ],
)
def test_engine_and_source_contract_must_be_an_exact_pair(
    write: EngineResultWrite,
    contract: str,
) -> None:
    _assert_invalid(replace(write, result=replace(write.result, contract=contract)))


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("team_id", "not-a-uuid"),
        ("analysis_id", "00000000-0000-4000-8000-000000000001"),
        ("execution_id", "00000000-0000-4000-8000-000000000101"),
        ("expected_execution_version", 0),
        ("expected_execution_version", True),
        ("tenant_resource_version", 0),
        ("tenant_resource_version", True),
        ("engine_id", "SmartPerfetto"),
        ("adapter_version", ""),
        ("adapter_version", "v" * 33),
        ("adapter_version", "bad version"),
        ("engine_commit_sha", "A" * 40),
        ("engine_commit_sha", "1" * 39),
        ("engine_image_digest", "1" * 64),
        ("engine_image_digest", f"sha256:{'G' * 64}"),
        ("attempt_number", 0),
        ("attempt_number", True),
        ("input_manifest_hash", "2" * 63),
        ("input_manifest_hash", "G" * 64),
        ("config_hash", "3" * 65),
        ("config_hash", "F" * 64),
    ],
)
def test_authoritative_identity_and_provenance_are_strict(
    field: str,
    invalid_value: object,
) -> None:
    _assert_invalid(replace(_smart_write(), **{field: invalid_value}))


def test_artifact_id_must_be_derived_from_the_execution_id() -> None:
    _assert_invalid(
        replace(
            _smart_write(),
            artifact_id=UUID("00000000-0000-4000-8000-000000000999"),
        )
    )


@pytest.mark.parametrize("invalid_value", [b"raw", bytearray(b"raw"), object()])
def test_non_json_values_are_rejected(invalid_value: object) -> None:
    _assert_invalid(_android_write(payload=_android_payload(value=invalid_value)))


def test_non_string_mapping_keys_are_rejected() -> None:
    payload = _android_payload()
    payload[1] = "not-json"  # type: ignore[index]
    _assert_invalid(_android_write(payload=payload))


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(invalid_value: float) -> None:
    _assert_invalid(_android_write(payload=_android_payload(value=invalid_value)))


@pytest.mark.parametrize("container_type", ["mapping", "list"])
def test_cyclic_structures_are_rejected(container_type: str) -> None:
    if container_type == "mapping":
        cycle: dict[str, Any] = {}
        cycle["cycle"] = cycle
    else:
        sequence: list[Any] = []
        sequence.append(sequence)
        cycle = {"cycle": sequence}

    _assert_invalid(_android_write(payload=_android_payload(extension=cycle)))


def test_depth_over_64_is_rejected() -> None:
    nested: dict[str, object] = {"leaf": "safe"}
    for _ in range(65):
        nested = {"nested": nested}

    _assert_invalid(_android_write(payload=_android_payload(deep=nested)))


def test_more_than_200_000_nodes_is_rejected_without_exceeding_collection_limit() -> None:
    payload = _android_payload(
        bulk={f"part-{index}": [0] * 50_000 for index in range(4)}
    )
    _assert_invalid(_android_write(payload=payload))


def test_collection_over_50_000_items_is_rejected() -> None:
    _assert_invalid(
        _android_write(payload=_android_payload(items=[None] * 50_001))
    )


def test_key_over_1_024_characters_is_rejected() -> None:
    marker = "k" * 1_025
    _assert_invalid(
        _android_write(payload=_android_payload(extension={marker: "safe"})),
        marker,
    )


def test_string_over_one_mib_is_rejected() -> None:
    marker = "unique-string-limit-marker"
    _assert_invalid(
        _android_write(payload=_android_payload(note="x" * (1024 * 1024) + marker)),
        marker,
    )


def test_canonical_envelope_over_two_mib_is_rejected() -> None:
    payload = _android_payload(
        first="a" * (1024 * 1024),
        second="b" * (1024 * 1024),
    )
    _assert_invalid(_android_write(payload=payload))


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "team_id",
        "bucket",
        "objectKey",
        "VersionId",
        "signed-url",
        "authorization",
        "apiKey",
        "api.key",
        "api/key",
        "api%5Fkey",
        "access_token",
        "client-secret",
        "client/secret",
        "password",
        "pass%77ord",
        "ｐａｓｓｗｏｒｄ",
        "private_key",
        "private.key",
        "secretAccessKey",
        "credential",
        "sessionToken",
        "session.token",
        "object.key",
        "team/id",
        "x-amz-credential",
    ],
)
def test_sensitive_keys_are_rejected_at_any_depth(sensitive_key: str) -> None:
    payload = _android_payload(nested={sensitive_key: "unique-sensitive-marker"})
    _assert_invalid(_android_write(payload=payload), "unique-sensitive-marker")


@pytest.mark.parametrize(
    "private_value",
    [
        "Bearer unique-credential-marker",
        "Basic dW5pcXVlOmNyZWRlbnRpYWw=",
        "Basic YTpi",
        "api_key=unique-credential-marker",
        "access_key=unique-credential-marker",
        "aws_access_key_id=unique-credential-marker",
        "aws_secret_access_key=unique-credential-marker",
        "client_secret=unique-credential-marker",
        "credential=unique-credential-marker",
        "private_key=unique-credential-marker",
        "refresh_token=unique-credential-marker",
        "secret_access_key=unique-credential-marker",
        '{"password":"unique-credential-marker"}',
        '{"client_secret":"unique-credential-marker"}',
        "-----BEGIN PRIVATE KEY-----\nunique-credential-marker\n-----END PRIVATE KEY-----",
        "https://user:password@example.test/private",
        "https://objects.test/item?X-Amz-Signature=unique-signature-marker",
        "https://objects.test/item?X-Goog-Signature=unique-signature-marker",
        "https://objects.test/item?X-Amz-Credential=unique-credential-marker",
        "https://objects.test/item?X-Goog-Credential=unique-credential-marker",
        "https://objects.test/item?token=unique-signature-marker",
        "token=unique-credential-marker",
        "s3://private-bucket/object",
        "GS://private-bucket/object",
        "az://private-container/object",
        "r2://private-bucket/object",
        "postgresql+psycopg://user:password@db.internal/app",
        "MySQL://user:password@db.internal/app",
        "redis://cache.internal/0",
        "FiLe:///private/worker.sock",
        "/Users/private/evidence.txt",
        "C:\\private\\evidence.txt",
        "\\private\\evidence.txt",
        "\\\\server\\share\\evidence.txt",
        "relative/../private/evidence.txt",
        "relative\\..\\private\\evidence.txt",
        "%2FUsers%2Fprivate%2Fevidence.txt",
        "%25252FUsers%25252Fprivate%25252Fevidence.txt",
    ],
)
def test_private_paths_credentials_and_storage_locations_are_rejected(
    private_value: str,
) -> None:
    _assert_invalid(
        _android_write(payload=_android_payload(note=private_value)),
        private_value,
    )


@pytest.mark.parametrize(
    "private_value",
    [
        "source=s3://private-bucket/object",
        "database=postgresql://user:pass@db.internal/app",
        "see https://user:pass@example.test/private",
    ],
)
def test_embedded_storage_and_credential_uris_are_rejected(private_value: str) -> None:
    _assert_invalid(
        _android_write(payload=_android_payload(note=private_value)),
        private_value,
    )


def test_privacy_decoding_fails_closed_when_the_bounded_passes_are_exhausted() -> None:
    private_value = "/Users/private/evidence.txt"
    for _ in range(10):
        private_value = quote(private_value, safe="")

    _assert_invalid(
        _android_write(payload=_android_payload(note=private_value)),
        private_value,
    )


def test_benign_relative_paths_and_descriptive_text_remain_allowed() -> None:
    payload = _android_payload(
        relative_path="evidence/meminfo.txt",
        note="object_key was omitted; database URL was not retained",
        evidence_summary=(
            "The report references /proc/meminfo without including a local path value."
        ),
        auth_note="Basic analysis confirmed that credentials were omitted",
    )

    canonical = canonicalize_engine_result(_android_write(payload=payload))

    result = canonical.document["result"]
    assert isinstance(result, dict)
    assert result["payload"] == payload


def test_smartperfetto_stable_payload_validator_returns_a_defensive_plain_copy() -> None:
    payload = _smart_payload(conclusion="稳定结论")

    validated = validate_sanitized_report_payload(payload)

    assert validated == payload
    assert validated is not payload
    assert validated["report"] is not payload["report"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reportId": "report-1", "report": {}, "extra": True},
        {"reportId": "bad report id", "report": {"reportId": "bad report id"}},
        {"reportId": "report-1", "report": {"reportId": "report-2"}},
        {"reportId": "report-1", "report": {"reportId": "report-1", "reportError": "raw"}},
        {"reportId": "report-1", "report": {"reportId": "report-1", "unexpected": "safe"}},
        {"reportId": "report-1", "report": {"reportId": "report-1", "objectKey": "x"}},
        {
            "reportId": "report-1",
            "report": {"reportId": "report-1", "note": "/private/evidence"},
        },
    ],
)
def test_smartperfetto_payload_must_be_an_already_sanitized_fixed_point(
    payload: object,
) -> None:
    with pytest.raises(ValueError, match="^report contract invalid$"):
        validate_sanitized_report_payload(payload)

    assert isinstance(payload, dict)
    _assert_invalid(_smart_write(payload=payload))


@pytest.mark.parametrize(
    "report_value",
    [
        float("nan"),
        "invalid-surrogate-\ud800-marker",
    ],
)
def test_smartperfetto_validator_redacts_serialization_failures(
    report_value: object,
) -> None:
    payload = {
        "reportId": "report-1",
        "report": {"reportId": "report-1", "summary": report_value},
    }

    with pytest.raises(ValueError) as raised:
        validate_sanitized_report_payload(payload)

    assert str(raised.value) == "report contract invalid"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("location", ["key", "value"])
def test_canonical_unicode_errors_have_no_explicit_exception_cause(
    location: str,
) -> None:
    marker = "invalid-surrogate-\ud800-marker"
    if location == "key":
        payload = _android_payload(extension={marker: "safe"})
    else:
        payload = _android_payload(note=marker)

    with pytest.raises(EngineResultValidationError) as raised:
        canonicalize_engine_result(_android_write(payload=payload))

    assert str(raised.value) == "engine result is invalid"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("context_type",), "android-memory-context"),
        (("schema_version",), "1.1"),
        (("generator", "name"), "other-generator"),
        (("generator", "version"), "1.2"),
        (("analysis_contract", "support_level"), "unknown"),
        (("analysis_contract", "privacy", "raw_contents_embedded"), True),
        (("analysis_contract", "privacy", "raw_contents_embedded"), 0),
        (("analysis_contract", "privacy", "local_paths_included"), True),
        (("analysis_contract", "privacy", "local_paths_included"), "false"),
    ],
)
def test_android_memory_payload_is_strict_and_requires_actual_false_privacy_flags(
    path: tuple[str, ...],
    invalid_value: object,
) -> None:
    payload = _android_payload()
    target: dict[str, object] = payload
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = invalid_value

    _assert_invalid(_android_write(payload=payload))


@pytest.mark.parametrize(
    "path",
    [
        ("analysis_contract", "privacy", "raw_contents_embedded"),
        ("analysis_contract", "privacy", "local_paths_included"),
    ],
)
def test_android_memory_payload_requires_both_privacy_flags(
    path: tuple[str, ...],
) -> None:
    payload = _android_payload()
    target: dict[str, object] = payload
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    del target[path[-1]]

    _assert_invalid(_android_write(payload=payload))


def test_validation_error_never_retains_supplied_detail() -> None:
    marker = "unique-validation-detail-marker"
    error = EngineResultValidationError(marker)

    assert str(error) == "engine result is invalid"
    assert marker not in repr(error)
