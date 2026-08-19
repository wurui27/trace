from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from perfpilot_agent.security import SourceFindingHint
from perfpilot_agent.source_snapshot import SourceExclusion, SourceSnapshot


@dataclass(frozen=True, slots=True)
class SourceRule:
    rule_id: str
    ranking_terms: tuple[str, ...]


ANDROID_RULES = (
    SourceRule(
        "android.startup.main_thread_io",
        ("StrictMode", "readBytes", "SQLiteDatabase"),
    ),
    SourceRule(
        "android.startup.eager_initialization",
        (
            "Application.onCreate",
            "override fun onCreate(",
            "ContentProvider",
            "Initializer",
        ),
    ),
    SourceRule(
        "android.ui.blocking_wait",
        ("runBlocking", "Thread.sleep", "Future.get", "CountDownLatch.await"),
    ),
    SourceRule(
        "android.compose.unstable_recomposition",
        ("@Composable", "remember", "derivedStateOf"),
    ),
    SourceRule(
        "android.memory.listener_leak",
        ("registerReceiver", "addObserver", "addListener"),
    ),
    SourceRule(
        "android.memory.bitmap_retention",
        ("Bitmap", "ImageDecoder", "LruCache"),
    ),
)


@dataclass(frozen=True, slots=True)
class SourceFragment:
    source_ref_id: UUID
    relative_path: str
    language: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    content_sha256: str
    snapshot_hash: str
    finding_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    rule_ids: tuple[str, ...]
    match_signals: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "source_ref_id": str(self.source_ref_id),
            "relative_path": self.relative_path,
            "language": self.language,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "snapshot_hash": self.snapshot_hash,
            "finding_ids": [str(item) for item in self.finding_ids],
            "evidence_ids": [str(item) for item in self.evidence_ids],
            "rule_ids": list(self.rule_ids),
            "match_signals": list(self.match_signals),
        }


@dataclass(frozen=True, slots=True)
class SourceContext:
    fragments: tuple[SourceFragment, ...]
    exclusions: tuple[SourceExclusion, ...]
    truncated: bool
    total_bytes: int
    canonical_bytes: bytes

    def fragment_documents(self) -> list[dict[str, object]]:
        return [fragment.document() for fragment in self.fragments]


@dataclass(frozen=True, slots=True)
class _Candidate:
    relative_path: str
    text: str
    rank: int
    symbol: str | None
    finding_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    rule_ids: tuple[str, ...]
    match_signals: tuple[str, ...]
    anchor_line: int


def _language(relative_path: str) -> str | None:
    lowered = relative_path.casefold()
    if lowered.endswith(".kt"):
        return "kotlin"
    if lowered.endswith(".java"):
        return "java"
    if lowered.endswith(".xml"):
        return "xml"
    if lowered.endswith(".gradle.kts") or lowered.endswith(".kts"):
        return "gradle_kts"
    if lowered.endswith(".gradle"):
        return "gradle"
    return None


def _symbol_terms(symbol: str) -> tuple[str, ...]:
    pieces = tuple(piece for piece in symbol.replace("#", ".").split(".") if piece)
    return tuple(dict.fromkeys((symbol, *pieces[-3:])))


_ANDROID_PACKAGE = re.compile(
    r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+\Z"
)
_PACKAGE_DECLARATION = re.compile(
    r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$",
    re.MULTILINE,
)
_TYPE_DECLARATION = re.compile(
    r"^\s*(?:(?:public|protected|private|internal|open|abstract|sealed|data|value|"
    r"inline|final)\s+)*(?:class|object|interface|record|enum\s+class)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_KOTLIN_FUNCTION = re.compile(
    r"^\s*(?:(?:public|protected|private|internal|open|abstract|override|suspend|"
    r"operator|infix|tailrec|external|inline|final)\s+)*fun\s+"
    r"(?:<[^>]+>\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_JAVA_METHOD = re.compile(
    r"^\s*(?:public|protected|private|static|final|synchronized|native|abstract|default)"
    r"(?:\s+(?:public|protected|private|static|final|synchronized|native|abstract|default))*"
    r"\s+[A-Za-z_][A-Za-z0-9_<>,.?\[\]]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)


def _without_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    in_block = False
    quote: str | None = None
    escaped = False
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_block:
            if current == "*" and following == "/":
                output.extend((" ", " "))
                index += 2
                in_block = False
                continue
            output.append("\n" if current == "\n" else " ")
            index += 1
            continue
        if quote is not None:
            output.append(current)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == quote:
                quote = None
            index += 1
            continue
        if current == "/" and following == "*":
            output.extend((" ", " "))
            index += 2
            in_block = True
            continue
        if current == "/" and following == "/":
            while index < len(text) and text[index] != "\n":
                output.append(" ")
                index += 1
            continue
        if current in {'"', "'"}:
            quote = current
        output.append(current)
        index += 1
    return "".join(output)


def _rule_source(text: str) -> str:
    return "\n".join(
        "" if line.lstrip().startswith(("import ", "package ")) else line
        for line in _without_comments(text).splitlines()
    )


def _android_package(value: str) -> str | None:
    package_name = value.split(":", 1)[0]
    if _ANDROID_PACKAGE.fullmatch(package_name) is None:
        return None
    return package_name


def _matches_android_component(relative_path: str, text: str, package_name: str) -> bool:
    escaped = re.escape(package_name)
    package_path = package_name.replace(".", "/").casefold()
    lowered_path = relative_path.casefold()
    return (
        f"/{package_path}/" in f"/{lowered_path}/"
        or re.search(rf"^\s*package\s+{escaped}(?:\s|$)", text, re.MULTILINE) is not None
        or re.search(rf"\bpackage\s*=\s*['\"]{escaped}['\"]", text) is not None
        or re.search(rf"\bapplicationId\s*(?:=\s*)?['\"]{escaped}['\"]", text)
        is not None
    )


def _concrete_symbol(text: str, *, relative_path: str, anchor_line: int) -> str | None:
    if not relative_path.casefold().endswith((".kt", ".java")):
        return None
    lines = text.splitlines()
    prefix = "\n".join(lines[: max(1, anchor_line)])
    package_match = _PACKAGE_DECLARATION.search(text)
    package_name = package_match.group(1) if package_match is not None else None
    type_matches = tuple(_TYPE_DECLARATION.finditer(prefix))
    function_pattern = (
        _KOTLIN_FUNCTION if relative_path.casefold().endswith(".kt") else _JAVA_METHOD
    )
    function_matches = tuple(function_pattern.finditer(prefix))
    parts = [package_name] if package_name is not None else []
    if type_matches:
        parts.append(type_matches[-1].group(1))
    if function_matches:
        parts.append(function_matches[-1].group(1))
    return ".".join(parts) if parts else None


def _matched_hints(
    relative_path: str,
    text: str,
    hints: Sequence[SourceFindingHint],
) -> tuple[tuple[SourceFindingHint, ...], str | None, bool, bool]:
    haystack = f"{relative_path}\n{_without_comments(text)}".casefold()
    direct: list[SourceFindingHint] = []
    component: list[SourceFindingHint] = []
    symbol: str | None = None
    for hint in hints:
        hint_direct = False
        hint_component = False
        for current in hint.symbol_hints:
            package_name = _android_package(current)
            if package_name is not None:
                if _matches_android_component(relative_path, text, package_name):
                    hint_component = True
                continue
            terms = _symbol_terms(current)
            lowered = tuple(item.casefold() for item in terms)
            terminal = lowered[-2:] if len(lowered) >= 2 else lowered
            if current.casefold() in haystack or terminal and all(term in haystack for term in terminal):
                hint_direct = True
                symbol = symbol or current
                break
            component_terms = tuple(term for term in lowered if len(term) >= 3)
            if component_terms and any(term in haystack for term in component_terms):
                hint_component = True
        if hint_direct:
            direct.append(hint)
        elif hint_component:
            component.append(hint)
    matched = tuple((*direct, *component))
    return matched, symbol, bool(direct), bool(component)


def _candidate(
    relative_path: str,
    text: str,
    hints: Sequence[SourceFindingHint],
) -> _Candidate:
    matched, symbol, direct, component = _matched_hints(relative_path, text, hints)
    source_for_rules = _rule_source(text)
    lowered = source_for_rules.casefold()
    rule_ids = tuple(
        rule.rule_id
        for rule in ANDROID_RULES
        if any(term.casefold() in lowered for term in rule.ranking_terms)
    )
    signals: list[str] = []
    if direct:
        signals.append("trace_symbol")
    if component:
        signals.append("android_component")
    if rule_ids:
        signals.append("android_rule")
    rank = (
        0
        if direct
        else 1
        if component and rule_ids
        else 2
        if component
        else 3
        if rule_ids
        else 4
    )
    lines = source_for_rules.splitlines()
    anchor_line = 1
    anchor_terms: tuple[str, ...] = ()
    if symbol is not None:
        anchor_terms = _symbol_terms(symbol)
    elif rule_ids:
        matched_rule_ids = set(rule_ids)
        anchor_terms = tuple(
            term
            for rule in ANDROID_RULES
            if rule.rule_id in matched_rule_ids
            for term in rule.ranking_terms
        )
    for line_number, line in enumerate(lines, start=1):
        lowered_line = line.casefold()
        if any(term.casefold() in lowered_line for term in anchor_terms):
            anchor_line = line_number
            break
    if symbol is None and component and rule_ids:
        symbol = _concrete_symbol(
            _without_comments(text),
            relative_path=relative_path,
            anchor_line=anchor_line,
        )
    finding_ids = tuple(dict.fromkeys(hint.finding_id for hint in matched))[:3]
    evidence_ids = tuple(
        dict.fromkeys(evidence for hint in matched for evidence in hint.evidence_ids)
    )[:20]
    return _Candidate(
        relative_path=relative_path,
        text=text,
        rank=rank,
        symbol=symbol,
        finding_ids=finding_ids,
        evidence_ids=evidence_ids,
        rule_ids=rule_ids[:8],
        match_signals=tuple(signals),
        anchor_line=anchor_line,
    )


def _bounded_fragment(
    text: str,
    *,
    maximum_bytes: int,
    anchor_line: int,
) -> tuple[str, int, int] | None:
    if maximum_bytes <= 0:
        return None
    all_lines = text.splitlines(keepends=True)
    start_index = max(0, min(anchor_line - 1, len(all_lines) - 1) - 80)
    lines = all_lines[start_index : start_index + 160]
    if not lines:
        return None
    selected: list[str] = []
    used = 0
    for line in lines:
        encoded = line.encode("utf-8")
        if used + len(encoded) > maximum_bytes:
            remaining = maximum_bytes - used
            if remaining > 0:
                clipped = encoded[:remaining]
                while clipped:
                    try:
                        decoded = clipped.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        clipped = clipped[:-1]
                        continue
                    if decoded:
                        selected.append(decoded)
                        used += len(clipped)
                    break
            break
        selected.append(line)
        used += len(encoded)
    content = "".join(selected)
    if not content:
        return None
    start_line = start_index + 1
    return content, start_line, start_line + (len(content.splitlines()) or 1) - 1


def select_source_context(
    snapshot: SourceSnapshot,
    finding_hints: Sequence[SourceFindingHint],
    *,
    max_files: int,
    max_bytes: int,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> SourceContext:
    if (
        len(finding_hints) > 3
        or not 1 <= max_files <= 12
        or not 1 <= max_bytes <= 98_304
    ):
        raise ValueError("source context limits are invalid")
    candidates = []
    exclusions = list(snapshot.exclusions)
    for item in snapshot.files:
        language = _language(item.relative_path)
        if language is None:
            exclusions.append(SourceExclusion(item.relative_path, "unsupported_language"))
            continue
        candidates.append(_candidate(item.relative_path, item.content.decode("utf-8"), finding_hints))
    candidates.sort(key=lambda item: (item.rank, item.relative_path))
    fragments: list[SourceFragment] = []
    total_bytes = 0
    truncated = len(candidates) > max_files
    for candidate in candidates:
        if len(fragments) >= max_files or total_bytes >= max_bytes:
            truncated = True
            break
        bounded = _bounded_fragment(
            candidate.text,
            maximum_bytes=max_bytes - total_bytes,
            anchor_line=candidate.anchor_line,
        )
        if bounded is None:
            truncated = True
            break
        content, start_line, end_line = bounded
        if len(content.encode("utf-8")) < len(candidate.text.encode("utf-8")):
            truncated = True
        source_ref_id = uuid_factory()
        if not isinstance(source_ref_id, UUID) or source_ref_id.version != 4:
            raise ValueError("source ref id is invalid")
        encoded = content.encode("utf-8")
        fragments.append(
            SourceFragment(
                source_ref_id=source_ref_id,
                relative_path=candidate.relative_path,
                language=_language(candidate.relative_path) or "kotlin",
                symbol=candidate.symbol,
                start_line=start_line,
                end_line=end_line,
                content=content,
                content_sha256=hashlib.sha256(encoded).hexdigest(),
                snapshot_hash=snapshot.snapshot_hash,
                finding_ids=candidate.finding_ids,
                evidence_ids=candidate.evidence_ids,
                rule_ids=candidate.rule_ids,
                match_signals=candidate.match_signals,
            )
        )
        total_bytes += len(encoded)
    exclusions.extend(
        SourceExclusion(relative_path, "tracked_deleted")
        for relative_path in snapshot.deleted_paths
    )
    exclusions = list(dict.fromkeys(exclusions))[:64]
    documents = [fragment.document() for fragment in fragments]
    canonical = json.dumps(
        documents,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return SourceContext(
        fragments=tuple(fragments),
        exclusions=tuple(exclusions),
        truncated=truncated,
        total_bytes=total_bytes,
        canonical_bytes=canonical,
    )


__all__ = [
    "ANDROID_RULES",
    "SourceContext",
    "SourceFragment",
    "SourceRule",
    "select_source_context",
]
