"""Deterministic Simplified-Chinese checks for user-facing AI narrative only."""

from __future__ import annotations

import re
from collections.abc import Mapping


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"\b[A-Za-z]{3,}\b")


class ChineseNarrativeError(ValueError):
    def __init__(self) -> None:
        super().__init__("ai_narrative_language_invalid")


def _texts(document: Mapping[str, object]) -> list[str]:
    values: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value)

    add(document.get("verdict"))
    add(document.get("executive_summary"))
    for item in document.get("top_findings", []):
        if isinstance(item, Mapping):
            add(item.get("user_impact"))
    for item in document.get("recommendations", []):
        if isinstance(item, Mapping):
            add(item.get("title"))
            add(item.get("action"))
            add(item.get("expected_effect"))
    for item in document.get("source_fixes", []):
        if isinstance(item, Mapping):
            add(item.get("diagnosis"))
            add(item.get("retest_target"))
    for item in document.get("retest_plan", []):
        if isinstance(item, Mapping):
            add(item.get("steps"))
    for item in document.get("limitations", []):
        if isinstance(item, Mapping):
            add(item.get("summary"))
    return values


def _acceptable(text: str) -> bool:
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN_WORD.findall(text))
    return cjk >= 2 and (latin == 0 or cjk >= latin * 2)


def validate_simplified_chinese_narrative(document: Mapping[str, object]) -> None:
    if not isinstance(document, Mapping) or any(
        not _acceptable(text) for text in _texts(document)
    ):
        raise ChineseNarrativeError


__all__ = ["ChineseNarrativeError", "validate_simplified_chinese_narrative"]
