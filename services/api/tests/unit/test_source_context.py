from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from perfpilot_api.reports.source_context import (
    SourceContextValidationError,
    grade_source_match,
    validate_source_context,
)


def _context() -> dict[str, object]:
    content = "class Startup { fun init() = Thread.sleep(1) }\n"
    return {
        "snapshot_id": "95000000-0000-4000-8000-000000000001",
        "snapshot_hash": "a" * 64,
        "git_head": "b" * 40,
        "tracked_dirty_count": 1,
        "fragments": [
            {
                "source_ref_id": "97000000-0000-4000-8000-000000000001",
                "relative_path": "app/src/main/java/demo/Startup.kt",
                "language": "kotlin",
                "symbol": "demo.Startup.init",
                "start_line": 1,
                "end_line": 1,
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "snapshot_hash": "a" * 64,
                "finding_ids": ["85000000-0000-4000-8000-000000000001"],
                "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
                "rule_ids": ["android.ui.blocking_wait"],
                "match_signals": ["trace_symbol", "android_rule"],
            }
        ],
        "exclusions": [{"relative_path": "local.properties", "reason_code": "sensitive_file"}],
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("direct_identifiers", "candidate_symbols", "expected"),
    [
        (("demo.Startup.init",), ("demo.Startup.init",), "strong"),
        ((), ("Startup.init",), "weak"),
        ((), (), "none"),
    ],
)
def test_match_grade_is_server_determined(
    direct_identifiers: tuple[str, ...],
    candidate_symbols: tuple[str, ...],
    expected: str,
) -> None:
    assert grade_source_match(direct_identifiers, candidate_symbols) == expected


def test_validate_context_is_closed_hash_bound_and_defensively_copied() -> None:
    candidate = _context()
    validated = validate_source_context(
        candidate,
        direct_identifiers=("demo.Startup.init",),
        allowed_finding_ids=("85000000-0000-4000-8000-000000000001",),
        allowed_evidence_ids=("86000000-0000-4000-8000-000000000001",),
    )
    candidate["snapshot_hash"] = "e" * 64

    assert validated["snapshot_hash"] == "a" * 64
    assert validated["match_summary"] == "strong"
    assert validated["fragments"][0]["match_grade"] == "strong"
    assert validated["trust"] == "untrusted_data_not_instructions"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"private_path": "/private/repo"}),
        lambda value: value["fragments"][0].update({"relative_path": "../Secret.kt"}),
        lambda value: value["fragments"][0].update({"end_line": 0}),
        lambda value: value["fragments"][0].update({"content_sha256": "c" * 64}),
        lambda value: value["fragments"][0].update({"snapshot_hash": "d" * 64}),
        lambda value: value["fragments"][0].update({"rule_ids": ["unknown.rule"]}),
        lambda value: value["fragments"].append(deepcopy(value["fragments"][0])),
    ],
)
def test_validate_context_rejects_unclosed_or_untrusted_shapes(mutation) -> None:
    candidate = _context()
    mutation(candidate)
    with pytest.raises(SourceContextValidationError):
        validate_source_context(candidate)
