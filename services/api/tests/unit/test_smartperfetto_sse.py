from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.sse import SseFrame, parse_sse_frames, project_sse_frame


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _parse(*chunks: bytes, max_event_bytes: int = 4096) -> list[SseFrame]:
    return [
        frame
        async for frame in parse_sse_frames(
            _chunks(*chunks),
            max_event_bytes=max_event_bytes,
        )
    ]


@pytest.mark.asyncio
async def test_incremental_parser_is_independent_of_every_byte_split() -> None:
    payload = (
        ": comment\r\n"
        "id: 7\r\n"
        "event: progress\r\n"
        "data: {\"content\":\r\n"
        "data: {\"progress\": 25}}\r\n"
        "\r\n"
    ).encode()
    expected = [
        SseFrame(
            event_id="7",
            event="progress",
            data='{"content":\n{"progress": 25}}',
        )
    ]

    assert await _parse(payload) == expected
    for split_at in range(len(payload) + 1):
        assert await _parse(payload[:split_at], payload[split_at:]) == expected


@pytest.mark.asyncio
async def test_parser_handles_utf8_comments_and_connected_without_cursor() -> None:
    payload = (
        ": 心跳\n"
        "event: connected\n"
        "data: {\"message\":\"连接成功\"}\n"
        "\n"
        "id: 0\n"
        "event: progress\n"
        "data: {\"content\":{\"progress\":0}}\n"
        "\n"
    ).encode("utf-8")

    frames = await _parse(*[payload[index : index + 1] for index in range(len(payload))])

    assert frames[0].event_id is None
    assert frames[0].event == "connected"
    assert frames[1].event_id == "0"


@pytest.mark.asyncio
async def test_final_incomplete_frame_is_not_committed() -> None:
    frames = await _parse(b'id: 1\nevent: progress\ndata: {"content":{"progress":50}}')

    assert frames == []


@pytest.mark.parametrize("event_id", ["-1", "+1", "01", "1.0", " 1", "1 ", "abc"])
@pytest.mark.asyncio
async def test_parser_rejects_noncanonical_event_ids_without_echoing_data(
    event_id: str,
) -> None:
    marker = "query-secret-marker"
    payload = (
        f"id: {event_id}\n"
        "event: progress\n"
        f'data: {{"query":"{marker}"}}\n\n'
    ).encode()

    with pytest.raises(EngineAdapterError) as exc_info:
        await _parse(payload)

    assert exc_info.value.stable_code == "engine_contract_invalid"
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_parser_rejects_invalid_utf8_and_oversized_frames() -> None:
    with pytest.raises(EngineAdapterError, match="engine adapter operation failed"):
        await _parse(b"event: progress\ndata: \xff\n\n")

    with pytest.raises(EngineAdapterError) as exc_info:
        await _parse(
            b"id: 1\nevent: progress\ndata: 123456789\n\n",
            max_event_bytes=8,
        )
    assert exc_info.value.stable_code == "engine_contract_invalid"


@pytest.mark.parametrize(
    ("progress", "expected"),
    [(None, None), (0, 0), (50, 50), (100, 100)],
)
def test_progress_projection_is_coarse_and_bounded(
    progress: int | None,
    expected: int | None,
) -> None:
    content = {} if progress is None else {"progress": progress}
    frame = SseFrame(
        event_id="1",
        event="progress",
        data=json.dumps({"content": content, "query": "must-not-copy"}),
    )

    event = project_sse_frame(frame, occurred_at=datetime(2026, 7, 28, tzinfo=UTC))

    assert event.progress_percent == expected
    assert event.message_code == "engine_progress"
    assert "must-not-copy" not in event.message_code


@pytest.mark.parametrize("progress", [-1, 101, 1.5, "50", True])
def test_progress_projection_rejects_invalid_values(progress: object) -> None:
    frame = SseFrame(
        event_id="1",
        event="progress",
        data=json.dumps({"content": {"progress": progress}, "error": "must-not-copy"}),
    )

    with pytest.raises(EngineAdapterError) as exc_info:
        project_sse_frame(frame, occurred_at=datetime(2026, 7, 28, tzinfo=UTC))
    assert exc_info.value.stable_code == "engine_contract_invalid"
    assert "must-not-copy" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("event_name", "state", "message_code"),
    [
        ("connected", "running", "engine_connected"),
        ("analysis_completed", "completed", "analysis_completed"),
        ("analysis_cancelled", "canceled", "analysis_canceled"),
        ("error", "failed", "engine_error"),
        ("end", "running", "stream_end"),
    ],
)
def test_projection_maps_only_stable_event_fields(
    event_name: str,
    state: str,
    message_code: str,
) -> None:
    frame = SseFrame(
        event_id="9",
        event=event_name,
        data='{"conclusion":"raw-secret-marker","error":"raw-error-marker"}',
    )

    event = project_sse_frame(frame, occurred_at=datetime(2026, 7, 28, tzinfo=UTC))

    assert event.event_id == "9"
    assert event.state == state
    assert event.message_code == message_code
    rendered = repr(event)
    assert "raw-secret-marker" not in rendered
    assert "raw-error-marker" not in rendered


def test_projection_rejects_malformed_json_without_echoing_it() -> None:
    frame = SseFrame(event_id="1", event="progress", data="query-secret-marker{")

    with pytest.raises(EngineAdapterError) as exc_info:
        project_sse_frame(frame, occurred_at=datetime(2026, 7, 28, tzinfo=UTC))

    assert exc_info.value.stable_code == "engine_contract_invalid"
    assert "query-secret-marker" not in str(exc_info.value)
    assert "query-secret-marker" not in repr(exc_info.value)
