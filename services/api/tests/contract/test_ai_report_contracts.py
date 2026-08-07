from __future__ import annotations

import base64
import hashlib
import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Callable

import jsonschema
import pytest

ROOT = Path(__file__).parents[4]
AI_SOURCE_FIX_FIELDS = (
    "fix_id",
    "finding_id",
    "evidence_ids",
    "recommendation_priority",
    "source_ref_ids",
    "rule_id",
    "match_grade",
    "relative_path",
    "symbol",
    "diagnosis",
    "diff",
    "validation_profile_id",
    "retest_target",
)
ALLOWED_SOURCE_FIX_EXTENSIONS = (".kt", ".java", ".xml")
FORBIDDEN_DIFF_METADATA_PREFIXES = (
    "Binary files ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


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
            if document.get("schema_version") == "1.2":
                _enforce_report_source_coherence(output, source_code)
                _enforce_verified_patch_artifacts(source_code)
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


def _enforce_report_source_coherence(
    synthesis_output: object,
    source_code: dict[str, object],
) -> None:
    source_refs = source_code.get("source_refs")
    public_fixes = source_code.get("fixes")
    if not isinstance(source_refs, list) or not isinstance(public_fixes, list):
        return

    ref_by_id: dict[object, dict[str, object]] = {}
    for source_ref in source_refs:
        if not isinstance(source_ref, dict):
            continue
        source_ref_id = source_ref.get("source_ref_id")
        if source_ref_id in ref_by_id:
            raise jsonschema.ValidationError("source ref IDs must be unique")
        ref_by_id[source_ref_id] = source_ref

    match_summary = source_code.get("match_summary")
    if match_summary == "weak" and any(
        source_ref.get("match_grade") != "weak" for source_ref in ref_by_id.values()
    ):
        raise jsonschema.ValidationError("weak match summary requires weak source refs")
    if match_summary == "none" and (source_refs or public_fixes):
        raise jsonschema.ValidationError("none match summary carries no source refs or fixes")
    if match_summary == "strong" and not any(
        source_ref.get("match_grade") == "strong"
        for source_ref in ref_by_id.values()
    ):
        raise jsonschema.ValidationError("strong match summary requires a strong source ref")

    snapshot = source_code.get("snapshot")
    snapshot_hash = snapshot.get("snapshot_hash") if isinstance(snapshot, dict) else None
    if any(
        source_ref.get("snapshot_hash") != snapshot_hash
        for source_ref in ref_by_id.values()
    ):
        raise jsonschema.ValidationError("source ref snapshot hash is incoherent")

    synthesis_fixes = (
        synthesis_output.get("source_fixes")
        if isinstance(synthesis_output, dict)
        else []
    )
    if not isinstance(synthesis_fixes, list):
        return
    fix_ids = [
        synthesis_fix.get("fix_id")
        for synthesis_fix in synthesis_fixes
        if isinstance(synthesis_fix, dict)
    ]
    if len(fix_ids) != len(set(fix_ids)):
        raise jsonschema.ValidationError("source fix IDs must be unique")
    if len(public_fixes) != len(synthesis_fixes):
        raise jsonschema.ValidationError(
            "report source fixes must exactly enrich synthesis source fixes"
        )

    report_profile_id = source_code.get("validation_profile_id")
    for synthesis_fix, public_fix in zip(synthesis_fixes, public_fixes, strict=True):
        if not isinstance(synthesis_fix, dict) or not isinstance(public_fix, dict):
            continue
        if any(
            synthesis_fix.get(field) != public_fix.get(field)
            for field in AI_SOURCE_FIX_FIELDS
        ):
            raise jsonschema.ValidationError(
                "report source fixes must exactly enrich synthesis source fixes in order"
            )

        for source_ref_id in synthesis_fix.get("source_ref_ids", []):
            source_ref = ref_by_id.get(source_ref_id)
            if source_ref is None:
                raise jsonschema.ValidationError("source fix has a dangling source ref")
            if source_ref.get("snapshot_hash") != snapshot_hash:
                raise jsonschema.ValidationError("source fix snapshot hash is incoherent")
            if source_ref.get("relative_path") != synthesis_fix.get("relative_path"):
                raise jsonschema.ValidationError("source fix path does not match source ref")
            if source_ref.get("symbol") != synthesis_fix.get("symbol"):
                raise jsonschema.ValidationError("source fix symbol does not match source ref")
            if synthesis_fix.get("finding_id") not in source_ref.get("finding_ids", []):
                raise jsonschema.ValidationError(
                    "source fix finding is not linked by source ref"
                )
            if not set(synthesis_fix.get("evidence_ids", [])).issubset(
                source_ref.get("evidence_ids", [])
            ):
                raise jsonschema.ValidationError(
                    "source fix evidence is not linked by source ref"
                )
            if synthesis_fix.get("rule_id") not in source_ref.get("rule_ids", []):
                raise jsonschema.ValidationError("source fix rule is not allowed by source ref")
            if not (
                synthesis_fix.get("match_grade")
                == source_ref.get("match_grade")
                == "strong"
            ):
                raise jsonschema.ValidationError("source fix requires a strong source ref")

        verification = public_fix.get("verification")
        if not isinstance(verification, dict):
            continue
        fix_profile_id = public_fix.get("validation_profile_id")
        if not (
            verification.get("profile_id")
            == fix_profile_id
            == report_profile_id
        ):
            raise jsonschema.ValidationError(
                "source fix verification profile is incoherent"
            )


def _enforce_verified_patch_artifacts(source_code: dict[str, object]) -> None:
    public_fixes = source_code.get("fixes")
    if not isinstance(public_fixes, list):
        return
    for public_fix in public_fixes:
        if not isinstance(public_fix, dict):
            continue
        verification = public_fix.get("verification")
        if not isinstance(verification, dict) or verification.get("state") != "verified":
            continue
        artifact = verification.get("patch_artifact")
        diff = public_fix.get("diff")
        if not isinstance(artifact, dict) or not isinstance(diff, str):
            continue
        diff_bytes = diff.encode("utf-8")
        digest = hashlib.sha256(diff_bytes).digest()
        if (
            artifact.get("size") != len(diff_bytes)
            or verification.get("patch_sha256") != digest.hex()
            or artifact.get("sha256_b64")
            != base64.b64encode(digest).decode("ascii")
            or artifact.get("mime") != "text/x-diff"
        ):
            raise jsonschema.ValidationError(
                "verified patch artifact does not match canonical UTF-8 diff bytes"
            )


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
        section_headers = [
            index for index, line in enumerate(lines) if line.startswith("diff --git ")
        ]
        if section_headers != [0]:
            raise jsonschema.ValidationError(
                "source fix must contain exactly one diff section"
            )
        old_file_headers = [
            index for index, line in enumerate(lines) if line.startswith("--- ")
        ]
        new_file_headers = [
            index for index, line in enumerate(lines) if line.startswith("+++ ")
        ]
        if old_file_headers != [1] or new_file_headers != [2]:
            raise jsonschema.ValidationError(
                "source fix must contain exactly one leading file header pair"
            )
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

        seen_hunk = False
        seen_change = False
        for line in lines[3:]:
            if line == "GIT binary patch" or line.startswith(
                FORBIDDEN_DIFF_METADATA_PREFIXES
            ):
                raise jsonschema.ValidationError(
                    "source fix contains forbidden diff metadata"
                )
            if re.fullmatch(
                r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?",
                line,
            ):
                seen_hunk = True
                continue
            if not seen_hunk:
                raise jsonschema.ValidationError("source fix has content before its hunk")
            if line.startswith(("+", "-")):
                seen_change = True
                continue
            if line.startswith(" ") or line == r"\ No newline at end of file":
                continue
            raise jsonschema.ValidationError("source fix has invalid hunk content")
        if not seen_hunk or not seen_change:
            raise jsonschema.ValidationError("source fix requires a complete changed hunk")


def _safe_relative_diff_path(path: str) -> bool:
    parts = path.split("/")
    return not (
        path.startswith("/")
        or re.match(r"^[A-Za-z]:/", path)
        or "\\" in path
        or "//" in path
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
        or not path.endswith(ALLOWED_SOURCE_FIX_EXTENSIONS)
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


def _bind_verified_patch_metadata(public_fix: dict[str, object]) -> None:
    diff_bytes = public_fix["diff"].encode("utf-8")  # type: ignore[union-attr]
    digest = hashlib.sha256(diff_bytes).digest()
    verification = public_fix["verification"]  # type: ignore[assignment]
    artifact = verification["patch_artifact"]
    verification["patch_sha256"] = digest.hex()
    artifact["sha256_b64"] = base64.b64encode(digest).decode("ascii")
    artifact["size"] = len(diff_bytes)


def _assert_only_report_v12_state(
    report: dict[str, object],
    expected_state: str,
) -> None:
    schema_name = "reports/analysis-report.schema.json"
    accepted_states = []
    for candidate_state in ("completed", "partially_completed", "failed", "canceled"):
        candidate = deepcopy(report)
        candidate["state"] = candidate_state
        try:
            _validate_ai_contract(schema_name, candidate)
        except jsonschema.ValidationError:
            continue
        accepted_states.append(candidate_state)
    assert accepted_states == [expected_state]


def _report_with_two_source_fixes() -> dict[str, object]:
    report = _completed_report_v12()
    synthesis_fix = deepcopy(report["synthesis"]["output"]["source_fixes"][0])  # type: ignore[index]
    synthesis_fix["fix_id"] = "96000000-0000-4000-8000-000000000002"
    report["synthesis"]["output"]["source_fixes"].append(synthesis_fix)  # type: ignore[index]

    public_fix = deepcopy(report["source_code"]["fixes"][0])  # type: ignore[index]
    public_fix["fix_id"] = synthesis_fix["fix_id"]
    public_fix["verification"]["patch_artifact"]["artifact_id"] = (  # type: ignore[index]
        "98000000-0000-4000-8000-000000000002"
    )
    report["source_code"]["fixes"].append(public_fix)  # type: ignore[index]
    return report


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


def _unsafe_complete_diff(kind: str, path: str) -> tuple[str, str]:
    declared_path = path
    if kind == "git-path":
        declared_path = ".git/hooks/post-commit.kt"
    elif kind == "casefold-git-path":
        declared_path = "tools/.GIT/hooks/post-commit.kt"

    diff = (
        f"diff --git a/{declared_path} b/{declared_path}\n"
        f"--- a/{declared_path}\n"
        f"+++ b/{declared_path}\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    if kind == "second-file":
        return declared_path, diff + (
            "diff --git a/src/Other.kt b/src/Other.kt\n"
            "--- a/src/Other.kt\n"
            "+++ b/src/Other.kt\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
        )
    if kind == "second-git-hook":
        return declared_path, diff + (
            "diff --git a/.git/hooks/post-commit b/.git/hooks/post-commit\n"
            "--- a/.git/hooks/post-commit\n"
            "+++ b/.git/hooks/post-commit\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
        )
    if kind == "traditional-second-file":
        return declared_path, diff + (
            "--- a/src/Other.kt\n"
            "+++ b/src/Other.kt\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
        )

    metadata = {
        "binary": "GIT binary patch\nliteral 0\n",
        "new-file": "new file mode 100644\n",
        "deleted-file": "deleted file mode 100644\n",
        "rename": "similarity index 100%\nrename from Old.kt\nrename to New.kt\n",
        "copy": "similarity index 100%\ncopy from Old.kt\ncopy to New.kt\n",
        "mode": "old mode 100644\nnew mode 100755\n",
        "symlink": "new file mode 120000\n",
    }.get(kind)
    if metadata is not None:
        first_hunk = diff.index("@@ ")
        diff = diff[:first_hunk] + metadata + diff[first_hunk:]
    return declared_path, diff


def _set_source_fix_diff_for_target(
    target: str,
    document: dict[str, object],
    declared_path: str,
    diff: str,
) -> None:
    if target == "standalone":
        fix = document["source_fixes"][0]  # type: ignore[index]
        fix["relative_path"] = declared_path
        fix["diff"] = diff
        return

    synthesis_fix = document["synthesis"]["output"]["source_fixes"][0]  # type: ignore[index]
    public_fix = document["source_code"]["fixes"][0]  # type: ignore[index]
    source_ref = document["source_code"]["source_refs"][0]  # type: ignore[index]
    for fix in (synthesis_fix, public_fix):
        fix["relative_path"] = declared_path
        fix["diff"] = diff
    source_ref["relative_path"] = declared_path
    _bind_verified_patch_metadata(public_fix)


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


@pytest.mark.parametrize(
    ("match_summary", "fragment_grades"),
    [
        ("none", ["strong"]),
        ("none", ["none"]),
        ("weak", []),
        ("weak", ["strong"]),
        ("weak", ["weak", "none"]),
        ("strong", []),
        ("strong", ["weak"]),
        ("strong", ["strong", "weak"]),
    ],
)
def test_projection_v2_match_summary_agrees_with_fragment_grades(
    match_summary: str,
    fragment_grades: list[str],
) -> None:
    projection = _example("analysis-projection-v2.valid.json")
    source_context = projection["source_context"]
    fragment = source_context["fragments"][0]
    source_context["match_summary"] = match_summary
    source_context["fragments"] = [
        {
            **deepcopy(fragment),
            "source_ref_id": f"97000000-0000-4000-8000-{index:012d}",
            "match_grade": grade,
        }
        for index, grade in enumerate(fragment_grades, start=1)
    ]
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("ai/analysis-projection.schema.json", projection)


@pytest.mark.parametrize(
    ("match_summary", "fragment_grades"),
    [("none", []), ("weak", ["weak"]), ("strong", ["strong", "strong"])],
)
def test_projection_v2_accepts_coherent_fragment_grades(
    match_summary: str,
    fragment_grades: list[str],
) -> None:
    projection = _example("analysis-projection-v2.valid.json")
    source_context = projection["source_context"]
    fragment = source_context["fragments"][0]
    source_context["match_summary"] = match_summary
    source_context["fragments"] = [
        {
            **deepcopy(fragment),
            "source_ref_id": f"97000000-0000-4000-8000-{index:012d}",
            "match_grade": grade,
        }
        for index, grade in enumerate(fragment_grades, start=1)
    ]
    _validate_ai_contract("ai/analysis-projection.schema.json", projection)


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
    if schema_name.startswith("reports/"):
        verification = deepcopy(document["source_code"]["fixes"][0]["verification"])
        document["source_code"]["fixes"] = [
            {**deepcopy(source_fix), "verification": deepcopy(verification)}
            for source_fix in output["source_fixes"]
        ]
        for public_fix in document["source_code"]["fixes"]:
            _bind_verified_patch_metadata(public_fix)
    _validate_ai_contract(schema_name, document)

    output["source_fixes"][1]["diff"] = _sized_unified_diff(path, 32_769)
    if schema_name.startswith("reports/"):
        document["source_code"]["fixes"][1]["diff"] = output["source_fixes"][1][  # type: ignore[index]
            "diff"
        ]
        _bind_verified_patch_metadata(document["source_code"]["fixes"][1])  # type: ignore[index]
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
    report["synthesis"]["output"]["source_fixes"] = [  # type: ignore[index]
        {field: deepcopy(public_fix[field]) for field in AI_SOURCE_FIX_FIELDS}
        for public_fix in report["source_code"]["fixes"]  # type: ignore[index]
    ]
    for public_fix in report["source_code"]["fixes"]:  # type: ignore[index]
        _bind_verified_patch_metadata(public_fix)
    _validate_ai_contract("reports/analysis-report.schema.json", report)

    report["source_code"]["fixes"][1]["diff"] = _sized_unified_diff(  # type: ignore[index]
        path,
        32_769,
    )
    report["synthesis"]["output"]["source_fixes"][1]["diff"] = (  # type: ignore[index]
        report["source_code"]["fixes"][1]["diff"]  # type: ignore[index]
    )
    _bind_verified_patch_metadata(report["source_code"]["fixes"][1])  # type: ignore[index]
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
    weak["source_code"]["source_refs"][0]["match_grade"] = "weak"
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
    failed_synthesis["source_code"]["fixes"] = []  # type: ignore[index]
    _validate_ai_contract(schema_name, failed_synthesis)


def test_report_v12_state_mapping_is_deterministic_for_source_and_core_outcomes() -> None:
    verified = _completed_report_v12()

    strong_without_fixes = deepcopy(verified)
    strong_without_fixes["source_code"]["fixes"] = []  # type: ignore[index]
    strong_without_fixes["synthesis"]["output"]["source_fixes"] = []  # type: ignore[index]

    weak = deepcopy(strong_without_fixes)
    weak["source_code"]["match_summary"] = "weak"  # type: ignore[index]
    weak["source_code"]["source_refs"][0]["match_grade"] = "weak"  # type: ignore[index]

    no_source = deepcopy(strong_without_fixes)
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

    unavailable = deepcopy(strong_without_fixes)
    unavailable["source_code"].update(  # type: ignore[union-attr]
        {
            "validation_profile_id": None,
            "snapshot": None,
            "context_state": "unavailable",
            "match_summary": "none",
            "source_refs": [],
            "fixes": [],
        }
    )

    pending = deepcopy(verified)
    _set_first_verification_state(pending, "pending")

    verification_not_requested = deepcopy(verified)
    _set_first_verification_state(verification_not_requested, "not_requested")

    verification_failed = deepcopy(verified)
    _set_first_verification_state(verification_failed, "validation_failed")
    verification_failed["source_code"]["fixes"][0]["verification"].update(  # type: ignore[index]
        {
            "exit_code": 1,
            "duration_ms": 1200,
            "log_summary": "Validation failed.",
        }
    )

    core_failed = _example("analysis-report-v1.2.valid.json")
    core_canceled = deepcopy(core_failed)
    core_canceled["scenario_reports"][0]["result_state"] = "canceled"  # type: ignore[index]

    verification_canceled = deepcopy(verified)
    _set_first_verification_state(verification_canceled, "canceled")

    cases = {
        "verified": (verified, "completed"),
        "verification-not-requested": (verification_not_requested, "completed"),
        "strong-without-fixes": (strong_without_fixes, "completed"),
        "weak": (weak, "completed"),
        "no-source": (no_source, "completed"),
        "unavailable": (unavailable, "partially_completed"),
        "verification-pending": (pending, "partially_completed"),
        "verification-failed": (verification_failed, "partially_completed"),
        "verification-canceled": (verification_canceled, "partially_completed"),
        "core-failed": (core_failed, "partially_completed"),
        "core-canceled": (core_canceled, "partially_completed"),
    }
    for report, expected_state in cases.values():
        _assert_only_report_v12_state(report, expected_state)


@pytest.mark.parametrize(
    "verification_state",
    [
        "not_requested",
        "pending",
        "validating",
        "verified",
        "apply_failed",
        "validation_failed",
        "source_changed",
        "not_configured",
        "timeout",
        "canceled",
        "unavailable",
    ],
)
def test_report_v12_every_verification_state_maps_to_exactly_one_report_state(
    verification_state: str,
) -> None:
    report = _completed_report_v12()
    if verification_state != "verified":
        _set_first_verification_state(report, verification_state)
    expected_state = (
        "completed"
        if verification_state in {"not_requested", "verified"}
        else "partially_completed"
    )
    _assert_only_report_v12_state(report, expected_state)


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


@pytest.mark.parametrize(
    ("match_summary", "ref_grade"),
    [("weak", "strong"), ("none", "strong"), ("strong", "weak")],
)
def test_report_v12_match_summary_agrees_with_source_ref_grades(
    match_summary: str,
    ref_grade: str,
) -> None:
    report = _completed_report_v12()
    report["source_code"]["match_summary"] = match_summary  # type: ignore[index]
    report["source_code"]["source_refs"][0]["match_grade"] = ref_grade  # type: ignore[index]
    report["source_code"]["fixes"] = []  # type: ignore[index]
    report["synthesis"]["output"]["source_fixes"] = []  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


def test_report_v12_strong_match_requires_a_strong_source_ref() -> None:
    report = _completed_report_v12()
    report["source_code"]["source_refs"] = []  # type: ignore[index]
    report["source_code"]["fixes"] = []  # type: ignore[index]
    report["synthesis"]["output"]["source_fixes"] = []  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


@pytest.mark.parametrize("identifier_kind", ["source-ref", "fix"])
def test_report_v12_source_ref_and_fix_ids_are_unique(identifier_kind: str) -> None:
    if identifier_kind == "source-ref":
        report = _completed_report_v12()
        duplicate_ref = deepcopy(report["source_code"]["source_refs"][0])  # type: ignore[index]
        duplicate_ref["end_line"] += 1
        duplicate_ref["content_sha256"] = "e" * 64
        report["source_code"]["source_refs"].append(duplicate_ref)  # type: ignore[index]
    else:
        report = _report_with_two_source_fixes()
        synthesis_fixes = report["synthesis"]["output"]["source_fixes"]  # type: ignore[index]
        public_fixes = report["source_code"]["fixes"]  # type: ignore[index]
        duplicate_id = synthesis_fixes[0]["fix_id"]
        for fix in (synthesis_fixes[1], public_fixes[1]):
            fix["fix_id"] = duplicate_id
            fix["diagnosis"] = "A distinct diagnosis with a duplicated fix identity."

    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


@pytest.mark.parametrize(
    "mutation",
    [
        "changed-public-fix-id",
        "dangling-source-ref",
        "mismatched-profile",
        "mismatched-path",
        "mismatched-symbol",
        "unreferenced-finding",
        "unreferenced-evidence",
        "mismatched-rule",
        "weak-referenced-ref",
        "reordered-public-fixes",
        "missing-public-fix",
        "extra-public-fix",
    ],
)
def test_report_v12_source_fixes_are_exact_ordered_enrichments(
    mutation: str,
) -> None:
    report = _report_with_two_source_fixes()
    synthesis_fixes = report["synthesis"]["output"]["source_fixes"]  # type: ignore[index]
    public_fixes = report["source_code"]["fixes"]  # type: ignore[index]

    if mutation == "changed-public-fix-id":
        public_fixes[0]["fix_id"] = "96000000-0000-4000-8000-000000000009"
    elif mutation == "dangling-source-ref":
        synthesis_fixes[0]["source_ref_ids"] = [
            "97000000-0000-4000-8000-000000000009"
        ]
    elif mutation == "mismatched-profile":
        public_fixes[0]["verification"]["profile_id"] = (  # type: ignore[index]
            "94000000-0000-4000-8000-000000000009"
        )
    elif mutation == "mismatched-path":
        other_path = "app/src/main/java/demo/OtherActivity.kt"
        other_diff = synthesis_fixes[0]["diff"].replace(  # type: ignore[union-attr]
            "app/src/main/java/demo/MainActivity.kt",
            other_path,
        )
        for fix in (synthesis_fixes[0], public_fixes[0]):
            fix["relative_path"] = other_path
            fix["diff"] = other_diff
    elif mutation == "mismatched-symbol":
        for fix in (synthesis_fixes[0], public_fixes[0]):
            fix["symbol"] = "demo.OtherActivity.onCreate"
    elif mutation == "unreferenced-finding":
        for fix in (synthesis_fixes[0], public_fixes[0]):
            fix["finding_id"] = "85000000-0000-4000-8000-000000000009"
    elif mutation == "unreferenced-evidence":
        for fix in (synthesis_fixes[0], public_fixes[0]):
            fix["evidence_ids"] = ["86000000-0000-4000-8000-000000000009"]
    elif mutation == "mismatched-rule":
        for fix in (synthesis_fixes[0], public_fixes[0]):
            fix["rule_id"] = "startup.unlisted_rule"
    elif mutation == "weak-referenced-ref":
        weak_ref = deepcopy(report["source_code"]["source_refs"][0])  # type: ignore[index]
        weak_ref["source_ref_id"] = "97000000-0000-4000-8000-000000000002"
        weak_ref["match_grade"] = "weak"
        report["source_code"]["source_refs"].append(weak_ref)  # type: ignore[index]
        for fix in (synthesis_fixes[0], public_fixes[0]):
            fix["source_ref_ids"].append(weak_ref["source_ref_id"])
    elif mutation == "reordered-public-fixes":
        report["source_code"]["fixes"] = list(reversed(public_fixes))  # type: ignore[index]
    elif mutation == "missing-public-fix":
        public_fixes.pop()
    else:
        extra = deepcopy(public_fixes[-1])
        extra["fix_id"] = "96000000-0000-4000-8000-000000000003"
        extra["verification"]["patch_artifact"]["artifact_id"] = (  # type: ignore[index]
            "98000000-0000-4000-8000-000000000003"
        )
        public_fixes.append(extra)

    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


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


def test_report_v12_verified_artifact_metadata_matches_canonical_utf8_diff() -> None:
    report = _completed_report_v12()
    public_fix = report["source_code"]["fixes"][0]  # type: ignore[index]
    verification = public_fix["verification"]
    artifact = verification["patch_artifact"]
    diff_bytes = public_fix["diff"].encode("utf-8")
    digest = hashlib.sha256(diff_bytes).digest()

    assert artifact["size"] == len(diff_bytes)
    assert verification["patch_sha256"] == digest.hex()
    assert artifact["sha256_b64"] == base64.b64encode(digest).decode("ascii")
    assert artifact["mime"] == "text/x-diff"


@pytest.mark.parametrize(
    "mutation",
    ["size", "patch-sha256", "artifact-sha256", "mime"],
)
def test_report_v12_verified_artifact_rejects_forged_diff_metadata(
    mutation: str,
) -> None:
    report = _completed_report_v12()
    verification = report["source_code"]["fixes"][0]["verification"]  # type: ignore[index]
    artifact = verification["patch_artifact"]
    if mutation == "size":
        artifact["size"] += 1
    elif mutation == "patch-sha256":
        verification["patch_sha256"] = "e" * 64
    elif mutation == "artifact-sha256":
        artifact["sha256_b64"] = "A" * 43 + "="
    else:
        artifact["mime"] = "text/plain"

    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


def test_report_v12_verified_artifact_size_uses_unicode_utf8_bytes() -> None:
    report = _completed_report_v12()
    synthesis_fix = report["synthesis"]["output"]["source_fixes"][0]  # type: ignore[index]
    public_fix = report["source_code"]["fixes"][0]  # type: ignore[index]
    unicode_diff = synthesis_fix["diff"].replace(  # type: ignore[union-attr]
        "+        deferSettingsLoad()\n",
        "+        deferSettingsLoad(\"界\")\n",
    )
    synthesis_fix["diff"] = unicode_diff
    public_fix["diff"] = unicode_diff
    _bind_verified_patch_metadata(public_fix)
    _validate_ai_contract("reports/analysis-report.schema.json", report)

    public_fix["verification"]["patch_artifact"]["size"] = len(unicode_diff)  # type: ignore[index]
    assert len(unicode_diff) < len(unicode_diff.encode("utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        _validate_ai_contract("reports/analysis-report.schema.json", report)


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


@pytest.mark.parametrize(
    "target",
    ["standalone", "report-synthesis", "report-public"],
)
@pytest.mark.parametrize(
    "unsafe_kind",
    [
        "second-file",
        "second-git-hook",
        "traditional-second-file",
        "git-path",
        "casefold-git-path",
        "binary",
        "new-file",
        "deleted-file",
        "rename",
        "copy",
        "mode",
        "symlink",
    ],
)
def test_source_fix_rejects_unsafe_content_anywhere_in_complete_diff(
    target: str,
    unsafe_kind: str,
) -> None:
    schema_name, document, fix = _source_fix_for_target(target)
    declared_path, diff = _unsafe_complete_diff(
        unsafe_kind,
        fix["relative_path"],  # type: ignore[arg-type]
    )
    _set_source_fix_diff_for_target(target, document, declared_path, diff)
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
    report["synthesis"]["output"]["source_fixes"] = [  # type: ignore[index]
        {field: deepcopy(public_fix[field]) for field in AI_SOURCE_FIX_FIELDS}
        for public_fix in source_code["fixes"]
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
