from __future__ import annotations

import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Callable

import jsonschema
import pytest

ROOT = Path(__file__).parents[4]


@lru_cache
def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "contracts/v1" / schema_name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _example(example_name: str) -> dict[str, object]:
    return json.loads((ROOT / "contracts/v1/examples" / example_name).read_text(encoding="utf-8"))


def _validate_ai_contract(schema_name: str, document: dict[str, object]) -> None:
    _validator(schema_name).validate(document)
    if schema_name == "ai/analysis-projection.schema.json":
        source_context = document.get("source_context")
        if isinstance(source_context, dict):
            _enforce_utf8_collection_limit(
                source_context.get("fragments"),
                field="content",
                limit=98_304,
                label="source fragments",
            )
        return
    output: object = document
    if schema_name == "reports/analysis-report.schema.json":
        synthesis = document.get("synthesis")
        output = synthesis.get("output") if isinstance(synthesis, dict) else None
        source_code = document.get("source_code")
        if isinstance(source_code, dict):
            _enforce_utf8_collection_limit(
                source_code.get("fixes"),
                field="diff",
                limit=65_536,
                label="report source fixes",
            )
            _enforce_unified_diff_headers(source_code.get("fixes"))
            snapshot = source_code.get("snapshot")
            source_refs = source_code.get("source_refs")
            if isinstance(snapshot, dict) and isinstance(source_refs, list):
                snapshot_hash = snapshot.get("snapshot_hash")
                if any(
                    isinstance(source_ref, dict)
                    and source_ref.get("snapshot_hash") != snapshot_hash
                    for source_ref in source_refs
                ):
                    raise jsonschema.ValidationError(
                        "source ref snapshot hash is incoherent"
                    )
    if isinstance(output, dict) and output.get("schema_version") == "2.0":
        _enforce_utf8_collection_limit(
            output.get("source_fixes"),
            field="diff",
            limit=65_536,
            label="synthesis source fixes",
        )
        _enforce_unified_diff_headers(output.get("source_fixes"))


def _enforce_utf8_collection_limit(
    items: object,
    *,
    field: str,
    limit: int,
    label: str,
) -> None:
    if not isinstance(items, list):
        return
    total = sum(
        len(item[field].encode("utf-8"))
        for item in items
        if isinstance(item, dict) and isinstance(item.get(field), str)
    )
    if total > limit:
        raise jsonschema.ValidationError(f"{label} exceed UTF-8 byte budget")


def _enforce_unified_diff_headers(items: object) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        relative_path = item.get("relative_path")
        diff = item.get("diff")
        if not isinstance(relative_path, str) or not isinstance(diff, str):
            continue
        lines = diff.splitlines()
        if len(lines) < 3:
            raise jsonschema.ValidationError("source fix is not a unified diff")
        matches = (
            re.fullmatch(r"diff --git a/([^\r\n]+) b/([^\r\n]+)", lines[0]),
            re.fullmatch(r"--- a/([^\r\n]+)", lines[1]),
            re.fullmatch(r"\+\+\+ b/([^\r\n]+)", lines[2]),
        )
        if any(match is None for match in matches):
            raise jsonschema.ValidationError("source fix is not a unified diff")
        header_paths = (
            matches[0].group(1),  # type: ignore[union-attr]
            matches[0].group(2),  # type: ignore[union-attr]
            matches[1].group(1),  # type: ignore[union-attr]
            matches[2].group(1),  # type: ignore[union-attr]
        )
        if any(not _safe_relative_diff_path(path) for path in header_paths):
            raise jsonschema.ValidationError("source fix has an unsafe diff path")
        if any(path != relative_path for path in header_paths):
            raise jsonschema.ValidationError("source fix diff headers do not match path")


def _safe_relative_diff_path(path: str) -> bool:
    return not (
        path.startswith("/")
        or re.match(r"^[A-Za-z]:/", path)
        or "\\" in path
        or "//" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    )


def _validation_keywords(
    errors: list[jsonschema.ValidationError],
) -> set[str]:
    keywords: set[str] = set()
    pending = list(errors)
    while pending:
        error = pending.pop()
        if isinstance(error.validator, str):
            keywords.add(error.validator)
        pending.extend(error.context)
    return keywords


def _sized_unified_diff(path: str, target_bytes: int) -> str:
    prefix = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "+"
    )
    suffix = "\n"
    fill_bytes = target_bytes - len((prefix + suffix).encode("utf-8"))
    assert fill_bytes >= 0
    return prefix + "界" * (fill_bytes // 3) + "x" * (fill_bytes % 3) + suffix


def _completed_report_v12() -> dict[str, object]:
    report = _example("analysis-report-v1.2.valid.json")
    completed_scenario = deepcopy(
        _example("analysis-report.partial.valid.json")["scenario_reports"][0]
    )
    completed_scenario["scenario_type"] = "startup"
    report["state"] = "completed"
    report["scenario_reports"] = [completed_scenario]
    return report


def _set_first_verification_state(
    report: dict[str, object],
    state: str,
) -> None:
    verification = report["source_code"]["fixes"][0]["verification"]  # type: ignore[index]
    verification.update(
        {
            "state": state,
            "exit_code": None,
            "duration_ms": None,
            "log_summary": None,
            "patch_artifact": None,
        }
    )


def _source_fix_for_target(
    target: str,
) -> tuple[str, dict[str, object], dict[str, object]]:
    if target == "standalone":
        schema_name = "ai/synthesis-output.schema.json"
        document = _example("synthesis-output-v2.valid.json")
        fix = document["source_fixes"][0]  # type: ignore[index]
    else:
        schema_name = "reports/analysis-report.schema.json"
        document = _completed_report_v12()
        if target == "report-synthesis":
            fix = document["synthesis"]["output"]["source_fixes"][0]  # type: ignore[index]
        else:
            fix = document["source_code"]["fixes"][0]  # type: ignore[index]
    return schema_name, document, fix


def _invalid_unified_diff(kind: str, path: str) -> str:
    if kind == "prose":
        return "Move the lookup after the first frame."
    if kind == "empty":
        return ""
    if kind == "absolute":
        invalid_path = "/private/MainActivity.kt"
    elif kind == "traversal":
        invalid_path = "../MainActivity.kt"
    else:
        return (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            "+++ b/app/src/main/java/demo/OtherActivity.kt\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
        )
    return (
        f"diff --git a/{invalid_path} b/{invalid_path}\n"
        f"--- a/{invalid_path}\n"
        f"+++ b/{invalid_path}\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("reports/normalized-trace-report.schema.json", "normalized-trace-report.valid.json"),
        ("ai/analysis-projection.schema.json", "analysis-projection.valid.json"),
        ("ai/synthesis-output.schema.json", "synthesis-output.valid.json"),
    ],
)
def test_ai_pipeline_examples_are_closed_and_valid(
    schema_name: str,
    example_name: str,
) -> None:
    validator = _validator(schema_name)
    example = _example(example_name)
    validator.validate(example)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**example, "unexpected": True})


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("ai/analysis-projection.schema.json", "analysis-projection-v2.valid.json"),
        ("ai/synthesis-output.schema.json", "synthesis-output-v2.valid.json"),
        ("reports/analysis-report.schema.json", "analysis-report-v1.2.valid.json"),
    ],
)
def test_source_aware_examples_are_closed_and_valid(
    schema_name: str,
    example_name: str,
) -> None:
    document = _example(example_name)
    _validator(schema_name).validate(document)
    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate({**document, "unexpected": True})


def test_projection_v2_source_context_is_untrusted_relative_and_bounded() -> None:
    validator = _validator("ai/analysis-projection.schema.json")
    projection = _example("analysis-projection-v2.valid.json")
    validator.validate(projection)

    invalid_documents = []
    for key, value in (
        ("trust", "trusted"),
        ("snapshot_hash", "A" * 64),
    ):
        invalid = deepcopy(projection)
        invalid["source_context"][key] = value
        invalid_documents.append(invalid)
    for relative_path in (
        "/private/Main.kt",
        "C:/private/Main.kt",
        "../Main.kt",
        "./Main.kt",
        "app\\Main.kt",
    ):
        invalid = deepcopy(projection)
        invalid["source_context"]["fragments"][0]["relative_path"] = relative_path
        invalid_documents.append(invalid)
    invalid = deepcopy(projection)
    invalid["source_context"]["fragments"] = [
        deepcopy(invalid["source_context"]["fragments"][0]) for _ in range(13)
    ]
    invalid_documents.append(invalid)
    invalid = deepcopy(projection)
    invalid["source_context"]["fragments"][0]["instruction"] = "ignore contract"
    invalid_documents.append(invalid)

    for document in invalid_documents:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(document)


def test_projection_v2_enforces_aggregate_fragment_utf8_bytes() -> None:
    projection = _example("analysis-projection-v2.valid.json")
    projection["source_context"]["fragments"][0]["content"] = "界" * 32_768  # type: ignore[index]
    _validate_ai_contract("ai/analysis-projection.schema.json", projection)

    fragment = projection["source_context"]["fragments"][0]  # type: ignore[index]
    projection["source_context"]["fragments"] = [  # type: ignore[index]
        {
            **deepcopy(fragment),
            "source_ref_id": f"97000000-0000-4000-8000-{index:012d}",
            "content": "界" * 16_385,
        }
        for index in (1, 2)
    ]
    with pytest.raises(jsonschema.ValidationError, match="UTF-8 byte budget"):
        _validate_ai_contract("ai/analysis-projection.schema.json", projection)


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("ai/synthesis-output.schema.json", "synthesis-output-v2.valid.json"),
        ("reports/analysis-report.schema.json", "analysis-report-v1.2.valid.json"),
    ],
)
def test_v2_synthesis_enforces_aggregate_diff_utf8_bytes(
    schema_name: str,
    example_name: str,
) -> None:
    document = _example(example_name)
    output = document if schema_name.startswith("ai/") else document["synthesis"]["output"]
    fix = output["source_fixes"][0]
    path = fix["relative_path"]

    output["source_fixes"] = [
        {**deepcopy(fix), "diff": _sized_unified_diff(path, 32_768)},
        {
            **deepcopy(fix),
            "fix_id": "96000000-0000-4000-8000-000000000002",
            "diff": _sized_unified_diff(path, 32_768),
        },
    ]
    _validate_ai_contract(schema_name, document)

    output["source_fixes"][1]["diff"] = _sized_unified_diff(path, 32_769)
    assert all(len(item["diff"]) <= 65_536 for item in output["source_fixes"])
    with pytest.raises(jsonschema.ValidationError, match="UTF-8 byte budget"):
        _validate_ai_contract(schema_name, document)


def test_report_v12_enforces_aggregate_public_fix_diff_utf8_bytes() -> None:
    report = _example("analysis-report-v1.2.valid.json")
    fix = report["source_code"]["fixes"][0]  # type: ignore[index]
    path = fix["relative_path"]
    report["source_code"]["fixes"] = [  # type: ignore[index]
        {**deepcopy(fix), "diff": _sized_unified_diff(path, 32_768)},
        {
            **deepcopy(fix),
            "fix_id": "96000000-0000-4000-8000-000000000002",
            "diff": _sized_unified_diff(path, 32_768),
        },
    ]
    _validate_ai_contract("reports/analysis-report.schema.json", report)

    report["source_code"]["fixes"][1]["diff"] = _sized_unified_diff(  # type: ignore[index]
        path,
        32_769,
    )
    with pytest.raises(jsonschema.ValidationError, match="UTF-8 byte budget"):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


def _assert_v2_synthesis_semantics(document: dict[str, object]) -> None:
    priorities = [item["priority"] for item in document["recommendations"]]  # type: ignore[index]
    assert len(priorities) == len(set(priorities))
    assert priorities == [priority for priority in ("p0", "p1", "p2") if priority in priorities]


def test_synthesis_v2_limits_paths_and_semantic_order() -> None:
    validator = _validator("ai/synthesis-output.schema.json")
    synthesis = _example("synthesis-output-v2.valid.json")
    validator.validate(synthesis)
    _assert_v2_synthesis_semantics(synthesis)

    for collection in (
        "key_metric_ids",
        "top_findings",
        "recommendations",
        "source_fixes",
        "retest_plan",
    ):
        invalid = deepcopy(synthesis)
        invalid[collection] = [deepcopy(invalid[collection][0]) for _ in range(4)]
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)

    invalid = deepcopy(synthesis)
    invalid["source_fixes"][0]["match_grade"] = "weak"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    for relative_path in ("../Main.kt", "C:/private/Main.kt", "./Main.kt"):
        invalid = deepcopy(synthesis)
        invalid["source_fixes"][0]["relative_path"] = relative_path
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)
    invalid = deepcopy(synthesis)
    invalid["source_fixes"][0]["source_ref_ids"] = [
        "97000000-0000-4000-8000-000000000001",
        "97000000-0000-4000-8000-000000000002",
        "97000000-0000-4000-8000-000000000003",
    ]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)

    duplicate_priority = deepcopy(synthesis)
    duplicate_priority["recommendations"] = [
        deepcopy(synthesis["recommendations"][0]),
        {
            **deepcopy(synthesis["recommendations"][1]),
            "priority": synthesis["recommendations"][0]["priority"],
        },
    ]
    with pytest.raises(AssertionError):
        _assert_v2_synthesis_semantics(duplicate_priority)
    reversed_priority = deepcopy(synthesis)
    reversed_priority["recommendations"] = list(
        reversed(reversed_priority["recommendations"])
    )
    with pytest.raises(AssertionError):
        _assert_v2_synthesis_semantics(reversed_priority)
def test_report_v12_source_states_never_erase_trace_facts() -> None:
    validator = _validator("reports/analysis-report.schema.json")
    report = _example("analysis-report-v1.2.valid.json")
    validator.validate(report)
    assert report["scenario_reports"]
    assert report["synthesis"]["output"]["schema_version"] == "2.0"
    assert "profile_id" in report["source_code"]["fixes"][0]["verification"]
    assert "validation_profile_id" not in report["source_code"]["fixes"][0][
        "verification"
    ]

    for state in ("pending", "validating"):
        pending = deepcopy(report)
        verification = pending["source_code"]["fixes"][0]["verification"]
        verification.update(
            {
                "state": state,
                "exit_code": None,
                "duration_ms": None,
                "log_summary": None,
                "patch_artifact": None,
            }
        )
        validator.validate(pending)

    failed = deepcopy(report)
    verification = failed["source_code"]["fixes"][0]["verification"]
    verification.update(
        {
            "state": "validation_failed",
            "exit_code": 1,
            "duration_ms": 1200,
            "log_summary": "Validation failed.",
            "patch_artifact": None,
        }
    )
    validator.validate(failed)

    weak = deepcopy(report)
    weak["source_code"]["match_summary"] = "weak"
    weak["source_code"]["fixes"] = []
    weak["synthesis"]["output"]["source_fixes"] = []
    validator.validate(weak)

    no_source = deepcopy(report)
    no_source["source_code"] = {
        "requested": False,
        "provider_kind": None,
        "agent_id": None,
        "workspace_id": None,
        "snapshot_policy": None,
        "validation_profile_id": None,
        "snapshot": None,
        "context_state": "not_requested",
        "match_summary": "none",
        "source_refs": [],
        "exclusions": [],
        "fixes": [],
        "limitations": [],
    }
    no_source["synthesis"]["output"]["source_fixes"] = []
    validator.validate(no_source)

    invalid = deepcopy(report)
    invalid["source_code"]["fixes"][0]["verification"]["patch_artifact"] = None
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = deepcopy(report)
    invalid["source_code"]["fixes"][0]["verification"]["state"] = "pending"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = deepcopy(report)
    invalid["synthesis"]["output"]["schema_version"] = "1.0"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    for relative_path in ("C:/private/MainActivity.kt", "./MainActivity.kt"):
        invalid = deepcopy(report)
        invalid["source_code"]["source_refs"][0]["relative_path"] = relative_path
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)


def test_report_v12_completed_requires_complete_core_synthesis_and_source() -> None:
    schema_name = "reports/analysis-report.schema.json"
    completed = _completed_report_v12()
    _validate_ai_contract(schema_name, completed)

    failed_scenario = deepcopy(completed)
    failed_scenario["scenario_reports"] = deepcopy(  # type: ignore[index]
        _example("analysis-report-v1.2.valid.json")["scenario_reports"]
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract(schema_name, failed_scenario)


def test_report_v12_completed_rejects_nonterminal_source_verification() -> None:
    schema_name = "reports/analysis-report.schema.json"
    completed = _completed_report_v12()
    pending_source = deepcopy(completed)
    _set_first_verification_state(pending_source, "pending")
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract(schema_name, pending_source)


def test_report_v12_completed_requires_completed_synthesis() -> None:
    schema_name = "reports/analysis-report.schema.json"
    completed = _completed_report_v12()
    failed_synthesis = deepcopy(completed)
    failed_synthesis["synthesis"] = {
        "state": "failed",
        "output": None,
        "synthesis_artifact_id": None,
        "failure_code": "ai_synthesis_failed",
        "provenance": None,
    }
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract(schema_name, failed_synthesis)


def test_report_v12_partial_requires_an_actual_partial_condition() -> None:
    schema_name = "reports/analysis-report.schema.json"
    all_complete = _completed_report_v12()
    all_complete["state"] = "partially_completed"
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract(schema_name, all_complete)


def test_report_v12_partial_accepts_source_or_synthesis_partial_condition() -> None:
    schema_name = "reports/analysis-report.schema.json"
    all_complete = _completed_report_v12()
    all_complete["state"] = "partially_completed"
    pending_source = deepcopy(all_complete)
    _set_first_verification_state(pending_source, "validating")
    _validate_ai_contract(schema_name, pending_source)

    failed_synthesis = deepcopy(all_complete)
    failed_synthesis["synthesis"] = {
        "state": "failed",
        "output": None,
        "synthesis_artifact_id": None,
        "failure_code": "ai_synthesis_failed",
        "provenance": None,
    }
    _validate_ai_contract(schema_name, failed_synthesis)


@pytest.mark.parametrize("source_case", ["not-requested", "weak", "none"])
def test_report_v12_rejects_synthesis_fixes_without_strong_source(
    source_case: str,
) -> None:
    report = _completed_report_v12()
    if source_case == "not-requested":
        report["source_code"] = {
            "requested": False,
            "provider_kind": None,
            "agent_id": None,
            "workspace_id": None,
            "snapshot_policy": None,
            "validation_profile_id": None,
            "snapshot": None,
            "context_state": "not_requested",
            "match_summary": "none",
            "source_refs": [],
            "exclusions": [],
            "fixes": [],
            "limitations": [],
        }
    else:
        report["source_code"]["match_summary"] = source_case  # type: ignore[index]
        report["source_code"]["fixes"] = []  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


@pytest.mark.parametrize("mutation", ["strong", "snapshot", "source-refs"])
def test_report_v12_unavailable_context_carries_no_source_identity(
    mutation: str,
) -> None:
    report = _completed_report_v12()
    source_code = report["source_code"]
    source_code.update(  # type: ignore[union-attr]
        {
            "context_state": "unavailable",
            "match_summary": "none",
            "snapshot": None,
            "source_refs": [],
            "fixes": [],
        }
    )
    report["synthesis"]["output"]["source_fixes"] = []  # type: ignore[index]
    if mutation == "strong":
        source_code["match_summary"] = "strong"  # type: ignore[index]
    elif mutation == "snapshot":
        source_code["snapshot"] = {  # type: ignore[index]
            "snapshot_id": "95000000-0000-4000-8000-000000000001",
            "snapshot_hash": "a" * 64,
            "git_head": "b" * 40,
        }
    else:
        source_code["source_refs"] = deepcopy(  # type: ignore[index]
            _example("analysis-report-v1.2.valid.json")["source_code"]["source_refs"]
        )
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


def test_report_v12_available_context_requires_snapshot() -> None:
    report = _completed_report_v12()
    report["source_code"]["fixes"] = []  # type: ignore[index]
    report["synthesis"]["output"]["source_fixes"] = []  # type: ignore[index]

    without_snapshot = deepcopy(report)
    without_snapshot["source_code"]["snapshot"] = None  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", without_snapshot)


def test_report_v12_source_refs_match_snapshot_hash() -> None:
    report = _completed_report_v12()
    report["source_code"]["fixes"] = []  # type: ignore[index]
    report["synthesis"]["output"]["source_fixes"] = []  # type: ignore[index]
    mismatched_hash = deepcopy(report)
    mismatched_hash["source_code"]["source_refs"][0]["snapshot_hash"] = "e" * 64  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError, match="snapshot hash"):
        _validate_ai_contract("reports/analysis-report.schema.json", mismatched_hash)


@pytest.mark.parametrize("state", ["pending", "validating"])
def test_report_v12_candidate_fix_has_no_artifact_before_verification(state: str) -> None:
    report = _completed_report_v12()
    report["state"] = "partially_completed"
    _set_first_verification_state(report, state)
    _validate_ai_contract("reports/analysis-report.schema.json", report)

    with_artifact = deepcopy(report)
    with_artifact["source_code"]["fixes"][0]["verification"]["patch_artifact"] = {  # type: ignore[index]
        "artifact_id": "98000000-0000-4000-8000-000000000001",
        "version_id": "source-patch-version-1",
        "sha256_b64": "A" * 43 + "=",
        "size": 241,
        "mime": "text/x-diff",
    }
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", with_artifact)


def test_report_v12_only_verified_fix_carries_artifact_identity() -> None:
    report = _completed_report_v12()
    _validate_ai_contract("reports/analysis-report.schema.json", report)

    missing_artifact = deepcopy(report)
    missing_artifact["source_code"]["fixes"][0]["verification"]["patch_artifact"] = None  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", missing_artifact)

    failed_with_artifact = deepcopy(report)
    failed_with_artifact["state"] = "partially_completed"
    verification = failed_with_artifact["source_code"]["fixes"][0]["verification"]  # type: ignore[index]
    verification.update(
        {
            "state": "validation_failed",
            "exit_code": 1,
            "duration_ms": 1200,
            "log_summary": "Validation failed.",
        }
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", failed_with_artifact)


@pytest.mark.parametrize(
    "target",
    ["standalone", "report-synthesis", "report-public"],
)
@pytest.mark.parametrize(
    "invalid_kind",
    ["prose", "empty", "absolute", "traversal", "mismatched-header"],
)
def test_source_fix_requires_safe_matching_unified_diff_headers(
    target: str,
    invalid_kind: str,
) -> None:
    schema_name, document, fix = _source_fix_for_target(target)
    _validate_ai_contract(schema_name, document)
    fix["diff"] = _invalid_unified_diff(invalid_kind, fix["relative_path"])  # type: ignore[arg-type]
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract(schema_name, document)


def test_synthesis_v2_collection_maxima_are_exact() -> None:
    schema_name = "ai/synthesis-output.schema.json"
    synthesis = _example("synthesis-output-v2.valid.json")

    synthesis["top_findings"] = [
        {
            **deepcopy(synthesis["top_findings"][0]),
            "finding_id": f"85000000-0000-4000-8000-{index:012d}",
        }
        for index in range(1, 4)
    ]
    synthesis["recommendations"] = [
        {
            **deepcopy(synthesis["recommendations"][0]),
            "priority": priority,
            "title": f"Recommendation {index}",
        }
        for index, priority in enumerate(("p0", "p1", "p2"), start=1)
    ]
    source_fix = synthesis["source_fixes"][0]
    synthesis["source_fixes"] = [
        {
            **deepcopy(source_fix),
            "fix_id": f"96000000-0000-4000-8000-{index:012d}",
        }
        for index in range(1, 4)
    ]
    synthesis["retest_plan"] = [
        {
            **deepcopy(synthesis["retest_plan"][0]),
            "steps": f"Retest step {index}",
        }
        for index in range(1, 4)
    ]
    synthesis["limitations"] = [
        {
            "limitation_id": f"87000000-0000-4000-8000-{index:012d}",
            "summary": f"Limitation {index}",
        }
        for index in range(1, 21)
    ]
    _validate_ai_contract(schema_name, synthesis)

    for collection in (
        "top_findings",
        "recommendations",
        "source_fixes",
        "retest_plan",
        "limitations",
    ):
        invalid = deepcopy(synthesis)
        extra = deepcopy(invalid[collection][0])
        if collection == "top_findings":
            extra["finding_id"] = "85000000-0000-4000-8000-000000000004"
        elif collection == "recommendations":
            extra["title"] = "Recommendation 4"
        elif collection == "source_fixes":
            extra["fix_id"] = "96000000-0000-4000-8000-000000000004"
        elif collection == "retest_plan":
            extra["steps"] = "Retest step 4"
        else:
            extra["limitation_id"] = "87000000-0000-4000-8000-000000000021"
        invalid[collection].append(extra)
        errors = list(_validator(schema_name).iter_errors(invalid))
        assert errors
        assert "maxItems" in _validation_keywords(errors)


def test_report_v12_source_collection_maxima_are_exact() -> None:
    schema_name = "reports/analysis-report.schema.json"
    report = _completed_report_v12()
    source_code = report["source_code"]
    source_ref = source_code["source_refs"][0]
    source_code["source_refs"] = [
        {
            **deepcopy(source_ref),
            "source_ref_id": f"97000000-0000-4000-8000-{index:012d}",
        }
        for index in range(1, 13)
    ]
    source_code["exclusions"] = [
        {
            "relative_path": f"src/excluded-{index}.kt",
            "reason_code": "excluded_file",
        }
        for index in range(1, 65)
    ]
    source_fix = source_code["fixes"][0]
    source_code["fixes"] = [
        {
            **deepcopy(source_fix),
            "fix_id": f"96000000-0000-4000-8000-{index:012d}",
        }
        for index in range(1, 4)
    ]
    source_code["limitations"] = [
        {
            "limitation_id": f"87000000-0000-4000-8000-{index:012d}",
            "summary": f"Limitation {index}",
        }
        for index in range(1, 21)
    ]
    _validate_ai_contract(schema_name, report)

    for collection in ("source_refs", "exclusions", "fixes", "limitations"):
        invalid = deepcopy(report)
        extra = deepcopy(invalid["source_code"][collection][0])  # type: ignore[index]
        if collection == "source_refs":
            extra["source_ref_id"] = "97000000-0000-4000-8000-000000000013"
        elif collection == "exclusions":
            extra["relative_path"] = "src/excluded-65.kt"
        elif collection == "fixes":
            extra["fix_id"] = "96000000-0000-4000-8000-000000000004"
        else:
            extra["limitation_id"] = "87000000-0000-4000-8000-000000000021"
        invalid["source_code"][collection].append(extra)  # type: ignore[index]
        errors = list(_validator(schema_name).iter_errors(invalid))
        assert errors
        assert "maxItems" in _validation_keywords(errors)


@pytest.mark.parametrize(
    ("schema_name", "example_name", "invalid_version"),
    [
        (
            "ai/analysis-projection.schema.json",
            "analysis-projection-v2.valid.json",
            "2.1",
        ),
        (
            "ai/synthesis-output.schema.json",
            "synthesis-output-v2.valid.json",
            "2.1",
        ),
        (
            "reports/analysis-report.schema.json",
            "analysis-report-v1.2.valid.json",
            "1.3",
        ),
    ],
)
def test_source_aware_contract_versions_are_exact(
    schema_name: str,
    example_name: str,
    invalid_version: str,
) -> None:
    document = _example(example_name)
    document["schema_version"] = invalid_version
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract(schema_name, document)


@pytest.mark.parametrize(
    ("schema_name", "example_name", "mutate"),
    [
        (
            "reports/normalized-trace-report.schema.json",
            "normalized-trace-report.valid.json",
            lambda value: value.__setitem__("schema_version", "2.0"),
        ),
        (
            "ai/analysis-projection.schema.json",
            "analysis-projection.valid.json",
            lambda value: value["scenarios"][0]["metrics"][0].__setitem__(
                "metric_id", "not-a-metric-uuid"
            ),
        ),
        (
            "ai/synthesis-output.schema.json",
            "synthesis-output.valid.json",
            lambda value: value["top_findings"][0].__setitem__("finding_id", "not-a-uuid"),
        ),
    ],
)
def test_ai_pipeline_contracts_reject_invalid_versions_numbers_and_references(
    schema_name: str,
    example_name: str,
    mutate: Callable[[dict[str, object]], object],
) -> None:
    value = deepcopy(_example(example_name))
    mutate(value)

    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate(value)


def test_synthesis_retest_modes_and_limits_are_strict() -> None:
    validator = _validator("ai/synthesis-output.schema.json")
    example = _example("synthesis-output.valid.json")
    invalid = deepcopy(example)
    invalid["retest_plan"][0]["limitation_ids"] = [
        "84000000-0000-4000-8000-000000000001"
    ]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)

    invalid = deepcopy(example)
    invalid["recommendations"] = [invalid["recommendations"][0]] * 11
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)


def test_canonical_validation_returns_a_defensive_copy_and_redacts_failures() -> None:
    from perfpilot_api.reports import (
        ReportContractError,
        canonical_json_bytes,
        validate_contract,
    )

    value = _example("synthesis-output.valid.json")
    result = validate_contract("synthesis-output", value)
    result["executive_summary"] = "changed"
    assert value["executive_summary"] != "changed"
    assert canonical_json_bytes({"b": "值", "a": 1}) == b'{"a":1,"b":"\xe5\x80\xbc"}'

    private_value = {"executive_summary": "private-token"}
    with pytest.raises(ReportContractError) as exc_info:
        validate_contract("synthesis-output", private_value)
    assert str(exc_info.value) == "report contract is invalid"
    assert "private-token" not in str(exc_info.value)


def test_canonical_validation_rejects_non_finite_numbers() -> None:
    from perfpilot_api.reports import ReportContractError, validate_contract

    value = _example("analysis-projection.valid.json")
    value["scenarios"][0]["metrics"][0]["numeric_value"] = float("nan")
    with pytest.raises(ReportContractError):
        validate_contract("analysis-projection", value)


def test_projection_uses_the_approved_source_and_scenarios_shape() -> None:
    validator = _validator("ai/analysis-projection.schema.json")
    projection = {
        "schema_version": "1.0",
        "analysis_id": "82000000-0000-4000-8000-000000000001",
        "analysis_profile": "auto",
        "question": None,
        "source": {
            "engine_id": "smartperfetto",
            "adapter_version": "1.0.0",
            "source_contract": "workspace-agent-v1",
            "canonical_artifact_id": "85000000-0000-4000-8000-000000000001",
        },
        "scenarios": [],
        "limitations": [],
    }
    validator.validate(projection)
    projection["source"]["engine_image_digest"] = "sha256:" + "1" * 64
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(projection)


def test_synthesis_matches_the_approved_object_and_retest_shapes() -> None:
    validator = _validator("ai/synthesis-output.schema.json")
    synthesis = {
        "schema_version": "1.0",
        "executive_summary": "Startup can be improved by moving a repeated lookup.",
        "top_findings": [
            {
                "finding_id": "85000000-0000-4000-8000-000000000001",
                "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
                "user_impact": "The first screen takes longer to appear.",
            }
        ],
        "recommendations": [
            {
                "priority": "p1",
                "title": "Move lookup off startup",
                "action": "Defer the lookup until after the first frame.",
                "expected_effect": "Reduce startup wait time.",
                "finding_ids": ["85000000-0000-4000-8000-000000000001"],
                "evidence_ids": ["86000000-0000-4000-8000-000000000001"],
            }
        ],
        "retest_plan": [
            {
                "mode": "collect_evidence",
                "scenario_type": "startup",
                "metric_ids": [],
                "limitation_ids": ["87000000-0000-4000-8000-000000000001"],
                "steps": "Capture five cold starts with the same recipe.",
                "success_condition": "evidence_collected",
                "failure_condition": "evidence_missing",
            }
        ],
        "limitations": [
            {
                "limitation_id": "87000000-0000-4000-8000-000000000001",
                "summary": "Only one valid startup sample is available.",
            }
        ],
    }
    validator.validate(synthesis)


@pytest.mark.parametrize(
    ("schema_name", "example_name", "uuid_path"),
    [
        (
            "reports/normalized-trace-report.schema.json",
            "normalized-trace-report.valid.json",
            ("analysis_id",),
        ),
        (
            "ai/analysis-projection.schema.json",
            "analysis-projection.valid.json",
            ("analysis_id",),
        ),
        (
            "ai/synthesis-output.schema.json",
            "synthesis-output.valid.json",
            ("top_findings", 0, "finding_id"),
        ),
    ],
)
def test_private_contracts_reject_noncanonical_uppercase_uuids(
    schema_name: str,
    example_name: str,
    uuid_path: tuple[str | int, ...],
) -> None:
    value: object = deepcopy(_example(example_name))
    target = value
    for key in uuid_path[:-1]:
        target = target[key]  # type: ignore[index]
    target[uuid_path[-1]] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        _validator(schema_name).validate(value)


def test_contract_names_are_closed_dashed_names_and_failures_are_redacted() -> None:
    from perfpilot_api.reports import ReportContractError, validate_contract

    fixtures = {
        "analysis-report": _example("analysis-report.trace-ai.valid.json"),
        "normalized-trace-report": _example("normalized-trace-report.valid.json"),
        "analysis-projection": _example("analysis-projection.valid.json"),
        "synthesis-output": _example("synthesis-output.valid.json"),
    }
    for name, value in fixtures.items():
        assert validate_contract(name, value) == value  # type: ignore[arg-type]
    with pytest.raises(ReportContractError, match="^report contract is invalid$"):
        validate_contract("../../private-document", fixtures["synthesis-output"])  # type: ignore[arg-type]


def test_normalized_trace_health_matches_the_closed_bundle_shape() -> None:
    validator = _validator("reports/normalized-trace-report.schema.json")
    report = _example("normalized-trace-report.valid.json")
    trace_health = report["scenario_reports"][0]["trace_health"]
    trace_health.update(
        {
            "target_resolution": {
                "package_name": "com.example.app",
                "process_name": "com.example.app",
                "upid": 17,
                "pid": 4242,
                "main_thread_id": 4242,
            },
            "measurement_window": {
                "start_ns": 100000000,
                "end_ns": 900000000,
                "coverage": "complete",
            },
            "data_loss": {
                "buffer_overruns": 0,
                "ftrace_events_lost": 0,
                "traced_buf_patches_failed": 0,
                "incomplete_slices": 0,
                "boundary_truncations": 0,
            },
        }
    )
    validator.validate(report)
    del trace_health["target_resolution"]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)


def test_contract_boundary_accepts_huge_finite_integers_without_overflow() -> None:
    from perfpilot_api.reports import validate_contract

    projection = _example("analysis-projection.valid.json")
    projection["scenarios"][0]["metrics"][0]["numeric_value"] = 10**1000
    validated = validate_contract("analysis-projection", projection)
    assert validated["scenarios"][0]["metrics"][0]["numeric_value"] == 10**1000
