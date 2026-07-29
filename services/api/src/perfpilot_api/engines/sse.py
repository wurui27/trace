"""Incremental, bounded parsing for SmartPerfetto server-sent events."""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from perfpilot_api.engines.contracts import EngineEvent, ExecutionStateValue
from perfpilot_api.engines.errors import EngineAdapterError


_CANONICAL_CURSOR = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_EVENT_PROJECTION: dict[str, tuple[ExecutionStateValue, str]] = {
    "connected": ("running", "engine_connected"),
    "progress": ("running", "engine_progress"),
    "analysis_completed": ("completed", "analysis_completed"),
    "analysis_cancelled": ("canceled", "analysis_canceled"),
    "error": ("failed", "engine_error"),
    "end": ("running", "stream_end"),
}


@dataclass(frozen=True, slots=True)
class SseFrame:
    event_id: str | None
    event: str
    data: str


def _contract_error() -> EngineAdapterError:
    return EngineAdapterError(
        stable_code="engine_contract_invalid",
        retryable=False,
    )


async def parse_sse_frames(
    chunks: AsyncIterable[bytes],
    *,
    max_event_bytes: int,
) -> AsyncIterator[SseFrame]:
    """Yield complete frames without retaining the complete response stream."""

    if max_event_bytes <= 0:
        raise ValueError("max_event_bytes must be positive")

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    text_buffer = ""
    event_bytes = 0
    event_id: str | None = None
    event_name = "message"
    data_lines: list[str] = []

    def consume_line(raw_line: str) -> SseFrame | None:
        nonlocal event_bytes, event_id, event_name, data_lines
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        event_bytes += len(raw_line.encode("utf-8")) + 1
        if event_bytes > max_event_bytes:
            raise _contract_error()

        if line == "":
            frame = None
            if data_lines:
                frame = SseFrame(
                    event_id=event_id,
                    event=event_name,
                    data="\n".join(data_lines),
                )
            event_bytes = 0
            event_id = None
            event_name = "message"
            data_lines = []
            return frame
        if line.startswith(":"):
            return None

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "id":
            if _CANONICAL_CURSOR.fullmatch(value) is None:
                raise _contract_error()
            event_id = value
        elif field == "event":
            if not value or "\x00" in value:
                raise _contract_error()
            event_name = value
        elif field == "data":
            data_lines.append(value)
        return None

    try:
        async for chunk in chunks:
            if not isinstance(chunk, bytes | bytearray):
                raise _contract_error()
            text_buffer += decoder.decode(bytes(chunk), final=False)
            while "\n" in text_buffer:
                raw_line, text_buffer = text_buffer.split("\n", 1)
                frame = consume_line(raw_line)
                if frame is not None:
                    yield frame
            if event_bytes + len(text_buffer.encode("utf-8")) > max_event_bytes:
                raise _contract_error()
        text_buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise _contract_error() from None

    # A frame is committed only by an empty line. Deliberately ignore any
    # incomplete tail so cancellation/connection loss cannot publish partial data.


def project_sse_frame(frame: SseFrame, *, occurred_at: datetime) -> EngineEvent:
    try:
        payload = json.loads(frame.data)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise _contract_error() from None
    if not isinstance(payload, dict):
        raise _contract_error()

    projection = _EVENT_PROJECTION.get(frame.event)
    if projection is None:
        raise _contract_error()
    state, message_code = projection

    if frame.event == "error":
        status = payload.get("status")
        if status == "quota_exceeded":
            state, message_code = "failed", "capacity_exceeded"
        elif status == "awaiting_user":
            state, message_code = "awaiting_user", "engine_interaction_required"

    progress: int | None = None
    if frame.event == "progress":
        content = payload.get("content")
        if content is not None and not isinstance(content, dict):
            raise _contract_error()
        candidate = content.get("progress") if isinstance(content, dict) else None
        if candidate is not None:
            if type(candidate) is not int or not 0 <= candidate <= 100:
                raise _contract_error()
            progress = candidate

    return EngineEvent(
        event_id=frame.event_id or "",
        state=state,
        progress_percent=progress,
        message_code=message_code,
        occurred_at=occurred_at,
    )


__all__ = ["SseFrame", "parse_sse_frames", "project_sse_frame"]
