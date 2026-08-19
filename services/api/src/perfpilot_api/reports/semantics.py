"""Semantic validation for source-aware report contracts.

JSON Schema closes the document shapes.  These checks cover invariants that
depend on UTF-8 byte counts, cryptographic digests, or relationships between
multiple parts of a document.
"""

from __future__ import annotations

import base64
import hashlib
import re


_AI_SOURCE_FIX_FIELDS = (
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
    "retest_target",
)
_ALLOWED_SOURCE_FIX_EXTENSIONS = (".kt", ".java", ".xml")
_FORBIDDEN_DIFF_METADATA_PREFIXES = (
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


class SourceAwareSemanticError(ValueError):
    """Raised internally when a source-aware semantic invariant is invalid."""


def validate_source_aware_semantics(
    name: str,
    document: dict[str, object],
) -> None:
    """Validate version-gated semantics that JSON Schema cannot express."""

    schema_version = document.get("schema_version")
    if name == "analysis-projection" and schema_version == "2.0":
        _validate_projection(document)
        return
    if name == "synthesis-output" and schema_version == "2.0":
        _validate_synthesis(document)
        return
    if name == "analysis-report" and schema_version == "1.2":
        _validate_report(document)


def _validate_projection(document: dict[str, object]) -> None:
    source_context = document.get("source_context")
    if not isinstance(source_context, dict):
        return
    fragments = source_context.get("fragments")
    _enforce_utf8_collection_limit(
        fragments,
        field="content",
        limit=98_304,
    )
    if not isinstance(fragments, list):
        return
    grades = [
        fragment.get("match_grade")
        for fragment in fragments
        if isinstance(fragment, dict)
    ]
    match_summary = source_context.get("match_summary")
    if match_summary == "none" and grades:
        raise SourceAwareSemanticError
    if match_summary == "weak" and (not grades or any(grade != "weak" for grade in grades)):
        raise SourceAwareSemanticError
    if match_summary == "strong" and (
        not grades or any(grade != "strong" for grade in grades)
    ):
        raise SourceAwareSemanticError


def _validate_synthesis(document: dict[str, object]) -> None:
    source_fixes = document.get("source_fixes")
    _enforce_utf8_collection_limit(
        source_fixes,
        field="diff",
        limit=65_536,
    )
    _enforce_unified_diff_headers(source_fixes)
    if not isinstance(source_fixes, list):
        return
    bindings: set[tuple[object, object, object]] = set()
    for source_fix in source_fixes:
        if not isinstance(source_fix, dict):
            continue
        binding = (
            source_fix.get("finding_id"),
            source_fix.get("relative_path"),
            source_fix.get("symbol"),
        )
        if binding in bindings:
            raise SourceAwareSemanticError
        bindings.add(binding)


def _validate_report(document: dict[str, object]) -> None:
    synthesis = document.get("synthesis")
    output = synthesis.get("output") if isinstance(synthesis, dict) else None
    if isinstance(output, dict) and output.get("schema_version") == "2.0":
        _validate_synthesis(output)

    source_code = document.get("source_code")
    if not isinstance(source_code, dict):
        return
    public_fixes = source_code.get("fixes")
    _enforce_utf8_collection_limit(
        public_fixes,
        field="diff",
        limit=65_536,
    )
    _enforce_unified_diff_headers(public_fixes)
    _enforce_report_source_coherence(output, source_code)
    _enforce_verified_patch_artifacts(source_code)


def _enforce_utf8_collection_limit(
    items: object,
    *,
    field: str,
    limit: int,
) -> None:
    if not isinstance(items, list):
        return
    total = sum(
        len(item[field].encode("utf-8"))
        for item in items
        if isinstance(item, dict) and isinstance(item.get(field), str)
    )
    if total > limit:
        raise SourceAwareSemanticError


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
            raise SourceAwareSemanticError
        ref_by_id[source_ref_id] = source_ref

    match_summary = source_code.get("match_summary")
    if match_summary == "weak" and any(
        source_ref.get("match_grade") != "weak" for source_ref in ref_by_id.values()
    ):
        raise SourceAwareSemanticError
    if match_summary == "none" and (source_refs or public_fixes):
        raise SourceAwareSemanticError
    if match_summary == "strong" and not any(
        source_ref.get("match_grade") == "strong" for source_ref in ref_by_id.values()
    ):
        raise SourceAwareSemanticError

    snapshot = source_code.get("snapshot")
    snapshot_hash = snapshot.get("snapshot_hash") if isinstance(snapshot, dict) else None
    if any(
        source_ref.get("snapshot_hash") != snapshot_hash
        for source_ref in ref_by_id.values()
    ):
        raise SourceAwareSemanticError

    conclusions = (
        synthesis_output.get("conclusions")
        if isinstance(synthesis_output, dict)
        else []
    )
    if not isinstance(conclusions, list):
        raise SourceAwareSemanticError
    for conclusion in conclusions:
        if not isinstance(conclusion, dict):
            raise SourceAwareSemanticError
        finding_id = conclusion.get("finding_id")
        evidence_ids = conclusion.get("evidence_ids")
        source_ref_ids = conclusion.get("source_ref_ids")
        if (
            not isinstance(evidence_ids, list)
            or not isinstance(source_ref_ids, list)
            or match_summary != "strong"
            and source_ref_ids
        ):
            raise SourceAwareSemanticError
        eligible_source_refs = {
            source_ref_id
            for source_ref_id, source_ref in ref_by_id.items()
            if source_ref.get("match_grade") == "strong"
            and finding_id in source_ref.get("finding_ids", [])
            and set(evidence_ids).issubset(source_ref.get("evidence_ids", []))
        }
        if eligible_source_refs and not source_ref_ids:
            raise SourceAwareSemanticError
        for source_ref_id in source_ref_ids:
            source_ref = ref_by_id.get(source_ref_id)
            if source_ref is None:
                raise SourceAwareSemanticError
            if (
                source_ref.get("match_grade") != "strong"
                or finding_id not in source_ref.get("finding_ids", [])
                or not set(evidence_ids).issubset(source_ref.get("evidence_ids", []))
            ):
                raise SourceAwareSemanticError

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
        raise SourceAwareSemanticError
    if len(public_fixes) != len(synthesis_fixes):
        raise SourceAwareSemanticError

    report_profile_id = source_code.get("validation_profile_id")
    for synthesis_fix, public_fix in zip(synthesis_fixes, public_fixes, strict=True):
        if not isinstance(synthesis_fix, dict) or not isinstance(public_fix, dict):
            continue
        if any(
            synthesis_fix.get(field) != public_fix.get(field)
            for field in _AI_SOURCE_FIX_FIELDS
        ):
            raise SourceAwareSemanticError

        source_ref_ids = synthesis_fix.get("source_ref_ids")
        if not isinstance(source_ref_ids, list):
            continue
        for source_ref_id in source_ref_ids:
            source_ref = ref_by_id.get(source_ref_id)
            if source_ref is None:
                raise SourceAwareSemanticError
            if source_ref.get("snapshot_hash") != snapshot_hash:
                raise SourceAwareSemanticError
            if source_ref.get("relative_path") != synthesis_fix.get("relative_path"):
                raise SourceAwareSemanticError
            if source_ref.get("symbol") != synthesis_fix.get("symbol"):
                raise SourceAwareSemanticError
            finding_ids = source_ref.get("finding_ids")
            if not isinstance(finding_ids, list) or synthesis_fix.get("finding_id") not in finding_ids:
                raise SourceAwareSemanticError
            fix_evidence_ids = synthesis_fix.get("evidence_ids")
            ref_evidence_ids = source_ref.get("evidence_ids")
            if not isinstance(fix_evidence_ids, list) or not isinstance(ref_evidence_ids, list):
                raise SourceAwareSemanticError
            if not set(fix_evidence_ids).issubset(ref_evidence_ids):
                raise SourceAwareSemanticError
            rule_ids = source_ref.get("rule_ids")
            if not isinstance(rule_ids, list) or synthesis_fix.get("rule_id") not in rule_ids:
                raise SourceAwareSemanticError
            if not (
                synthesis_fix.get("match_grade")
                == source_ref.get("match_grade")
                == "strong"
            ):
                raise SourceAwareSemanticError

        verification = public_fix.get("verification")
        if not isinstance(verification, dict):
            continue
        fix_profile_id = public_fix.get("validation_profile_id")
        if verification.get("profile_id") != fix_profile_id or fix_profile_id != report_profile_id:
            raise SourceAwareSemanticError


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
            or artifact.get("sha256_b64") != base64.b64encode(digest).decode("ascii")
            or artifact.get("mime") != "text/x-diff"
        ):
            raise SourceAwareSemanticError


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
            raise SourceAwareSemanticError
        section_headers = [
            index for index, line in enumerate(lines) if line.startswith("diff --git ")
        ]
        if section_headers != [0]:
            raise SourceAwareSemanticError
        old_file_headers = [
            index for index, line in enumerate(lines) if line.startswith("--- ")
        ]
        new_file_headers = [
            index for index, line in enumerate(lines) if line.startswith("+++ ")
        ]
        if old_file_headers != [1] or new_file_headers != [2]:
            raise SourceAwareSemanticError
        matches = (
            re.fullmatch(r"diff --git a/([^\r\n]+) b/([^\r\n]+)", lines[0]),
            re.fullmatch(r"--- a/([^\r\n]+)", lines[1]),
            re.fullmatch(r"\+\+\+ b/([^\r\n]+)", lines[2]),
        )
        if any(match is None for match in matches):
            raise SourceAwareSemanticError
        header_paths = (
            matches[0].group(1),  # type: ignore[union-attr]
            matches[0].group(2),  # type: ignore[union-attr]
            matches[1].group(1),  # type: ignore[union-attr]
            matches[2].group(1),  # type: ignore[union-attr]
        )
        if any(not _safe_relative_diff_path(path) for path in header_paths):
            raise SourceAwareSemanticError
        if any(path != relative_path for path in header_paths):
            raise SourceAwareSemanticError

        seen_hunk = False
        seen_change = False
        for line in lines[3:]:
            if line == "GIT binary patch" or line.startswith(
                _FORBIDDEN_DIFF_METADATA_PREFIXES
            ):
                raise SourceAwareSemanticError
            if re.fullmatch(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?", line):
                seen_hunk = True
                continue
            if not seen_hunk:
                raise SourceAwareSemanticError
            if line.startswith(("+", "-")):
                seen_change = True
                continue
            if line.startswith(" ") or line == r"\ No newline at end of file":
                continue
            raise SourceAwareSemanticError
        if not seen_hunk or not seen_change:
            raise SourceAwareSemanticError


def _safe_relative_diff_path(path: str) -> bool:
    parts = path.split("/")
    return not (
        path.startswith("/")
        or re.match(r"^[A-Za-z]:/", path)
        or "\\" in path
        or "//" in path
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
        or not path.endswith(_ALLOWED_SOURCE_FIX_EXTENSIONS)
    )


__all__ = ["SourceAwareSemanticError", "validate_source_aware_semantics"]
