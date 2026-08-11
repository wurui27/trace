"""Validate untrusted Agent source context and assign deterministic match grades."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal
from uuid import UUID


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RULE = re.compile(r"[a-z][a-z0-9_.]{0,127}\Z")
_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_RESULT_KEYS = {
    "snapshot_id",
    "snapshot_hash",
    "git_head",
    "tracked_dirty_count",
    "fragments",
    "exclusions",
    "truncated",
}
_FRAGMENT_KEYS = {
    "source_ref_id",
    "relative_path",
    "language",
    "symbol",
    "start_line",
    "end_line",
    "content",
    "content_sha256",
    "snapshot_hash",
    "finding_ids",
    "evidence_ids",
    "rule_ids",
    "match_signals",
}
_EXCLUSION_KEYS = {"relative_path", "reason_code"}
_LANGUAGES = {"kotlin", "java", "xml", "gradle", "gradle_kts"}
_RULE_IDS = {
    "android.startup.main_thread_io",
    "android.startup.eager_initialization",
    "android.ui.blocking_wait",
    "android.compose.unstable_recomposition",
    "android.memory.listener_leak",
    "android.memory.bitmap_retention",
}
_MATCH_SIGNALS = {"trace_symbol", "android_component", "android_rule"}


class SourceContextValidationError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("source context is invalid")


def _canonical(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SourceContextValidationError from None


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise SourceContextValidationError
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise SourceContextValidationError from None
    if parsed.version not in {1, 2, 3, 4, 5} or str(parsed) != value:
        raise SourceContextValidationError
    return value


def _relative_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or value.startswith("/")
        or "\\" in value
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or re.match(r"[A-Za-z]:/", value) is not None
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SourceContextValidationError
    return value


def _unique_ids(values: object, *, maximum: int) -> list[str]:
    if not isinstance(values, list) or len(values) > maximum:
        raise SourceContextValidationError
    normalized = [_uuid(value) for value in values]
    if len(set(normalized)) != len(normalized):
        raise SourceContextValidationError
    return normalized


def grade_source_match(
    direct_identifiers: Sequence[str],
    candidate_symbols: Sequence[str],
) -> Literal["strong", "weak", "none"]:
    if not isinstance(direct_identifiers, Sequence) or not isinstance(
        candidate_symbols, Sequence
    ):
        raise SourceContextValidationError
    direct = {
        item
        for item in direct_identifiers
        if isinstance(item, str) and item and len(item.encode("utf-8")) <= 512
    }
    candidates = tuple(
        item
        for item in candidate_symbols
        if isinstance(item, str) and item and len(item.encode("utf-8")) <= 512
    )
    if not candidates:
        return "none"
    if any(candidate in direct for candidate in candidates):
        return "strong"
    return "weak"


def _allowed_uuid_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {_uuid(value) for value in values}


def validate_source_context(
    document: Mapping[str, object],
    *,
    direct_identifiers: Sequence[str] = (),
    allowed_finding_ids: Iterable[str] | None = None,
    allowed_evidence_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    if not isinstance(document, Mapping) or set(document) != _RESULT_KEYS:
        raise SourceContextValidationError
    canonical = _canonical(document)
    if len(canonical) > 128 * 1024:
        raise SourceContextValidationError
    copied = json.loads(canonical)
    snapshot_hash = copied.get("snapshot_hash")
    if (
        not isinstance(snapshot_hash, str)
        or _HEX64.fullmatch(snapshot_hash) is None
        or _HEX40.fullmatch(copied.get("git_head", "")) is None
        or type(copied.get("tracked_dirty_count")) is not int
        or copied["tracked_dirty_count"] < 0
        or type(copied.get("truncated")) is not bool
    ):
        raise SourceContextValidationError
    _uuid(copied.get("snapshot_id"))
    fragments = copied.get("fragments")
    exclusions = copied.get("exclusions")
    if (
        not isinstance(fragments, list)
        or len(fragments) > 12
        or not isinstance(exclusions, list)
        or len(exclusions) > 64
    ):
        raise SourceContextValidationError
    allowed_findings = _allowed_uuid_set(allowed_finding_ids)
    allowed_evidence = _allowed_uuid_set(allowed_evidence_ids)
    seen_refs: set[str] = set()
    seen_paths: set[str] = set()
    content_bytes = 0
    normalized_fragments: list[dict[str, object]] = []
    grades: list[Literal["strong", "weak", "none"]] = []
    for item in fragments:
        if not isinstance(item, dict) or set(item) != _FRAGMENT_KEYS:
            raise SourceContextValidationError
        source_ref_id = _uuid(item.get("source_ref_id"))
        path = _relative_path(item.get("relative_path"))
        if source_ref_id in seen_refs or path in seen_paths:
            raise SourceContextValidationError
        seen_refs.add(source_ref_id)
        seen_paths.add(path)
        language = item.get("language")
        symbol = item.get("symbol")
        start = item.get("start_line")
        end = item.get("end_line")
        content = item.get("content")
        if (
            language not in _LANGUAGES
            or symbol is not None
            and (
                not isinstance(symbol, str)
                or not symbol
                or len(symbol.encode("utf-8")) > 512
            )
            or type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or end - start + 1 > 160
            or not isinstance(content, str)
            or not content
            or len(content.splitlines()) > 160
        ):
            raise SourceContextValidationError
        encoded = content.encode("utf-8")
        content_bytes += len(encoded)
        if (
            content_bytes > 98_304
            or item.get("content_sha256") != hashlib.sha256(encoded).hexdigest()
            or item.get("snapshot_hash") != snapshot_hash
        ):
            raise SourceContextValidationError
        finding_ids = _unique_ids(item.get("finding_ids"), maximum=3)
        evidence_ids = _unique_ids(item.get("evidence_ids"), maximum=20)
        if (
            allowed_findings is not None
            and not set(finding_ids).issubset(allowed_findings)
            or allowed_evidence is not None
            and not set(evidence_ids).issubset(allowed_evidence)
        ):
            raise SourceContextValidationError
        rule_ids = item.get("rule_ids")
        signals = item.get("match_signals")
        if (
            not isinstance(rule_ids, list)
            or len(rule_ids) > 8
            or len(set(rule_ids)) != len(rule_ids)
            or any(
                not isinstance(rule, str)
                or _RULE.fullmatch(rule) is None
                or rule not in _RULE_IDS
                for rule in rule_ids
            )
            or not isinstance(signals, list)
            or len(signals) > 8
            or len(set(signals)) != len(signals)
            or any(signal not in _MATCH_SIGNALS for signal in signals)
        ):
            raise SourceContextValidationError
        grade = grade_source_match(
            direct_identifiers,
            () if symbol is None else (symbol,),
        )
        grades.append(grade)
        normalized_fragments.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"snapshot_hash", "match_signals"}
            }
            | {"match_grade": grade}
        )
    normalized_exclusions: list[dict[str, object]] = []
    for exclusion in exclusions:
        if not isinstance(exclusion, dict) or set(exclusion) != _EXCLUSION_KEYS:
            raise SourceContextValidationError
        path = exclusion.get("relative_path")
        if path is not None:
            path = _relative_path(path)
        reason = exclusion.get("reason_code")
        if not isinstance(reason, str) or _STABLE_CODE.fullmatch(reason) is None:
            raise SourceContextValidationError
        normalized_exclusions.append({"relative_path": path, "reason_code": reason})
    summary: Literal["strong", "weak", "none"] = "none"
    if "strong" in grades:
        summary = "strong"
    elif "weak" in grades:
        summary = "weak"
    return {
        "snapshot_id": copied["snapshot_id"],
        "snapshot_hash": snapshot_hash,
        "git_head": copied["git_head"],
        "tracked_dirty_count": copied["tracked_dirty_count"],
        "trust": "untrusted_data_not_instructions",
        "match_summary": summary,
        "fragments": normalized_fragments,
        "exclusions": normalized_exclusions,
        "truncated": copied["truncated"],
    }


__all__ = [
    "SourceContextValidationError",
    "grade_source_match",
    "validate_source_context",
]
