from __future__ import annotations

import hashlib
import json
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
        ("Application.onCreate", "ContentProvider", "Initializer"),
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


def _matched_hints(
    relative_path: str,
    text: str,
    hints: Sequence[SourceFindingHint],
) -> tuple[tuple[SourceFindingHint, ...], str | None, bool, bool]:
    haystack = f"{relative_path}\n{text}".casefold()
    direct: list[SourceFindingHint] = []
    component: list[SourceFindingHint] = []
    symbol: str | None = None
    for hint in hints:
        hint_direct = False
        hint_component = False
        for current in hint.symbol_hints:
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
    lowered = text.casefold()
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
    rank = 0 if direct else 1 if component else 2 if rule_ids else 3
    lines = text.splitlines()
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
