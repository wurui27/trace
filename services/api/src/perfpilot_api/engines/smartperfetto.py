"""SmartPerfetto adapter for verified Trace submission."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError

from perfpilot_api.engines.contracts import (
    AdapterDescriptor,
    EngineEvent,
    EngineEventBatch,
    EngineInput,
    EngineResult,
    EngineRunRef,
    EngineStatus,
    EngineTerminalStateValue,
    SubmitConfig,
)
from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.smartperfetto_contracts import (
    SmartPerfettoAnalyzeResponse,
    SmartPerfettoCancelResponse,
    SmartPerfettoEndpointError,
    SmartPerfettoReportResponse,
    SmartPerfettoResumeResponse,
    SmartPerfettoScenePreview,
    SmartPerfettoStatusResponse,
    SmartPerfettoTraceUploadResponse,
)
from perfpilot_api.engines.smartperfetto_transport import (
    SmartPerfettoTransport,
    validate_external_id,
)
from perfpilot_api.engines.sse import parse_sse_frames, project_sse_frame


_STARTUP_QUERY = (
    "Analyze Android application startup performance, identify root causes, "
    "and recommend concrete optimizations."
)
_SCROLL_QUERY = (
    "Analyze Android scrolling performance, identify jank root causes, and "
    "recommend concrete optimizations."
)
_PREVIEW_QUERY = "Inventory supported startup and scroll scenes."
_DEEP_DIVE_QUERY = "Analyze the selected PerfPilot performance scenes."
_SUPPORTED_SCENE_TYPES = (
    "cold_start",
    "warm_start",
    "hot_start",
    "scroll",
    "inertial_scroll",
)
_TRACE_MIME_TYPES = {
    "application/octet-stream",
    "application/x-perfetto-trace",
}


def _error(
    stable_code: str,
    *,
    retryable: bool = False,
    terminal_state: str | None = "failed",
) -> EngineAdapterError:
    return EngineAdapterError(
        stable_code=stable_code,
        retryable=retryable,
        terminal_state=terminal_state,  # type: ignore[arg-type]
    )


def _append_context(query: str, question: str | None) -> str:
    if question is None or not question.strip():
        return query
    context = question.strip()
    if len(context) > 2_000:
        raise _error("engine_input_invalid")
    return f"{query}\n\nAdditional analysis context: {context}"


class SmartPerfettoAdapter:
    descriptor = AdapterDescriptor(
        engine_id="smartperfetto",
        adapter_version="1.0.0",
        profiles=frozenset({"auto", "startup", "scroll"}),
        required_inputs=frozenset({"trace"}),
        optional_inputs=frozenset(),
        accepted_contracts=frozenset({"workspace-agent-v1"}),
        default_timeout_seconds=1_800,
        resource_profile="network_service",
        stable_error_codes=frozenset(
            {
                "artifact_unavailable",
                "capacity_exceeded",
                "engine_auth_failed",
                "engine_contract_invalid",
                "engine_input_invalid",
                "engine_interaction_required",
                "engine_failed",
                "engine_quota_exceeded",
                "engine_session_lost",
                "engine_tenant_unavailable",
                "engine_timeout",
                "engine_timeout_invalid",
                "engine_unavailable",
                "engine_workspace_missing",
                "trace_integrity_failed",
                "unsupported_trace_profile",
            }
        ),
    )

    def __init__(
        self,
        *,
        transport: SmartPerfettoTransport,
        artifact_client: httpx.AsyncClient,
        max_trace_bytes: int,
        max_timeout_seconds: int,
        max_sse_event_bytes: int,
        spool_max_memory_bytes: int = 8 * 1024 * 1024,
        spool_factory: Callable[..., AbstractContextManager[BinaryIO]] = (
            tempfile.SpooledTemporaryFile
        ),
        max_sse_batch_events: int = 64,
        max_sse_batch_seconds: float = 2.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if artifact_client.follow_redirects:
            raise ValueError("artifact client must not follow redirects")
        if min(
            max_trace_bytes,
            max_timeout_seconds,
            max_sse_event_bytes,
            spool_max_memory_bytes,
            max_sse_batch_events,
            max_sse_batch_seconds,
        ) <= 0:
            raise ValueError("SmartPerfetto adapter bounds must be positive")
        self._transport = transport
        self._artifact_client = artifact_client
        self._max_trace_bytes = max_trace_bytes
        self._max_timeout_seconds = max_timeout_seconds
        self._max_sse_event_bytes = max_sse_event_bytes
        self._spool_max_memory_bytes = spool_max_memory_bytes
        self._spool_factory = spool_factory
        self._max_sse_batch_events = max_sse_batch_events
        self._max_sse_batch_seconds = max_sse_batch_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef:
        trace, workspace_id = self._validate_submit(inputs, config)
        try:
            async with asyncio.timeout(config.timeout_seconds):
                with self._spool_factory(
                    max_size=self._spool_max_memory_bytes,
                    mode="w+b",
                ) as materialized:
                    await self._materialize(trace, materialized)
                    trace_id = await self._upload(
                        materialized,
                        workspace_id=workspace_id,
                    )
                    if config.profile == "auto":
                        return await self._submit_auto(
                            trace_id=trace_id,
                            workspace_id=workspace_id,
                            question=config.question,
                        )
                    query = (
                        _STARTUP_QUERY
                        if config.profile == "startup"
                        else _SCROLL_QUERY
                    )
                    return await self._analyze(
                        workspace_id=workspace_id,
                        trace_id=trace_id,
                        query=_append_context(query, config.question),
                        options={"analysisMode": "full"},
                    )
        except TimeoutError:
            raise _error("engine_timeout", retryable=True, terminal_state=None) from None

    def _validate_submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> tuple[EngineInput, str]:
        if len(inputs) != 1:
            raise _error("engine_input_invalid")
        trace = inputs[0]
        if (
            trace.kind != "trace"
            or trace.mime not in _TRACE_MIME_TYPES
            or type(trace.size_bytes) is not int
            or not 0 < trace.size_bytes <= self._max_trace_bytes
        ):
            raise _error("engine_input_invalid")
        try:
            digest = base64.b64decode(trace.sha256_b64, validate=True)
        except (ValueError, binascii.Error):
            raise _error("engine_input_invalid") from None
        if (
            len(digest) != hashlib.sha256().digest_size
            or base64.b64encode(digest).decode("ascii") != trace.sha256_b64
        ):
            raise _error("engine_input_invalid")
        if config.external_workspace_id is None:
            raise _error("engine_workspace_missing")
        try:
            workspace_id = validate_external_id(config.external_workspace_id)
        except EngineAdapterError:
            raise _error("engine_workspace_missing") from None
        if (
            type(config.timeout_seconds) is not int
            or not 0 < config.timeout_seconds <= self._max_timeout_seconds
        ):
            raise _error("engine_timeout_invalid")
        return trace, workspace_id

    async def _materialize(self, trace: EngineInput, destination: BinaryIO) -> None:
        raw_url = trace.download_url.get_secret_value()
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            raise _error("artifact_unavailable", retryable=True) from None
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise _error("artifact_unavailable", retryable=True)

        response: httpx.Response | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            request = self._artifact_client.build_request("GET", raw_url)
            response = await self._artifact_client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if not 200 <= response.status_code <= 299:
                raise _error("artifact_unavailable", retryable=True)
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_trace_bytes or size > trace.size_bytes:
                    raise _error("trace_integrity_failed")
                digest.update(chunk)
                destination.write(chunk)
        except EngineAdapterError:
            raise
        except httpx.TimeoutException:
            raise _error("artifact_unavailable", retryable=True) from None
        except httpx.RequestError:
            raise _error("artifact_unavailable", retryable=True) from None
        finally:
            if response is not None:
                await response.aclose()

        expected_digest = base64.b64decode(trace.sha256_b64, validate=True)
        if size != trace.size_bytes or not hmac.compare_digest(
            digest.digest(),
            expected_digest,
        ):
            raise _error("trace_integrity_failed")
        destination.seek(0)

    async def _upload(self, trace_file: BinaryIO, *, workspace_id: str) -> str:
        filename = f"perfpilot-trace-{uuid4()}.pftrace"
        response = await self._transport.request_multipart_json(
            f"/api/workspaces/{workspace_id}/traces/upload",
            workspace_id=workspace_id,
            filename=filename,
            file=trace_file,
        )
        try:
            parsed = SmartPerfettoTraceUploadResponse.model_validate(response.payload)
        except ValidationError:
            raise self._endpoint_error(
                operation="upload",
                status_code=response.status_code,
                payload=response.payload,
            ) from None
        if not 200 <= response.status_code <= 299:
            raise self._endpoint_error(
                operation="upload",
                status_code=response.status_code,
                payload=response.payload,
            )
        return parsed.trace.id

    async def _analyze(
        self,
        *,
        workspace_id: str,
        trace_id: str,
        query: str,
        options: dict[str, object],
    ) -> EngineRunRef:
        response = await self._transport.request_json(
            "POST",
            f"/api/workspaces/{workspace_id}/agent/analyze",
            workspace_id=workspace_id,
            json_body={
                "traceId": trace_id,
                "query": query,
                "options": options,
            },
        )
        try:
            parsed = SmartPerfettoAnalyzeResponse.model_validate(response.payload)
        except ValidationError:
            raise self._endpoint_error(
                operation="analyze",
                status_code=response.status_code,
                payload=response.payload,
            ) from None
        if not 200 <= response.status_code <= 299:
            raise self._endpoint_error(
                operation="analyze",
                status_code=response.status_code,
                payload=response.payload,
            )
        return EngineRunRef(
            engine_id="smartperfetto",
            external_session_id=parsed.session_id,
            external_run_id=parsed.run_id,
            cursor=None,
            external_workspace_id=workspace_id,
        )

    async def stream(
        self,
        run_ref: EngineRunRef,
        cursor: str | None,
    ) -> EngineEventBatch:
        current = self._validate_run_ref(run_ref, require_run=True)
        if cursor is not None:
            try:
                cursor_value = int(cursor)
            except ValueError:
                raise _error("engine_contract_invalid") from None
            if str(cursor_value) != cursor or cursor_value < 0:
                raise _error("engine_contract_invalid")
        else:
            cursor_value = -1

        for attempt in range(2):
            run_id = current.external_run_id
            workspace_id = current.external_workspace_id
            assert run_id is not None and workspace_id is not None
            async with self._transport.stream_response(
                f"/api/workspaces/{workspace_id}/agent/runs/{run_id}/stream",
                workspace_id=workspace_id,
                last_event_id=cursor,
            ) as response:
                if response.status_code == 404:
                    if attempt == 1:
                        raise _error("engine_session_lost")
                elif response.status_code != 200:
                    raise self._endpoint_error(
                        operation="stream",
                        status_code=response.status_code,
                        payload={},
                    )
                else:
                    events, last_cursor, saw_end = await self._read_event_batch(
                        response,
                        after_cursor=cursor_value,
                    )
                    refreshed = replace(
                        current,
                        cursor=last_cursor if last_cursor is not None else cursor,
                    )
                    if saw_end or not events:
                        status = await self.status(refreshed)
                        refreshed = replace(
                            status.run_ref,
                            cursor=refreshed.cursor,
                        )
                    return EngineEventBatch(run_ref=refreshed, events=events)
            current = await self._resume(current)
        raise _error("engine_session_lost")

    async def _read_event_batch(
        self,
        response: httpx.Response,
        *,
        after_cursor: int,
    ) -> tuple[tuple[EngineEvent, ...], str | None, bool]:
        events: list[EngineEvent] = []
        last_value = after_cursor
        last_cursor: str | None = None
        saw_end = False
        try:
            async with asyncio.timeout(self._max_sse_batch_seconds):
                async for frame in parse_sse_frames(
                    response.aiter_bytes(),
                    max_event_bytes=self._max_sse_event_bytes,
                ):
                    if frame.event_id is None:
                        continue
                    event_value = int(frame.event_id)
                    if event_value <= last_value:
                        continue
                    event = project_sse_frame(frame, occurred_at=self._now())
                    events.append(event)
                    last_value = event_value
                    last_cursor = frame.event_id
                    saw_end = frame.event == "end"
                    if (
                        len(events) >= self._max_sse_batch_events
                        or event.state in {"completed", "failed", "canceled"}
                        or saw_end
                    ):
                        break
        except TimeoutError:
            pass
        return tuple(events), last_cursor, saw_end

    async def status(self, run_ref: EngineRunRef) -> EngineStatus:
        response, current = await self._request_json_with_resume(
            run_ref,
            method="GET",
            path=lambda ref: self._session_path(ref, "status"),
        )
        try:
            parsed = SmartPerfettoStatusResponse.model_validate(response.payload)
        except ValidationError:
            raise _error("engine_contract_invalid") from None
        if parsed.session_id != current.external_session_id:
            raise _error("engine_session_lost")
        mapped = {
            "pending": ("pending", None, False),
            "running": ("running", None, False),
            "awaiting_user": (
                "awaiting_user",
                "engine_interaction_required",
                False,
            ),
            "completed": ("completed", None, False),
            "failed": ("failed", "engine_failed", False),
            "cancelled": ("canceled", None, False),
            "quota_exceeded": ("failed", "capacity_exceeded", True),
        }
        state, stable_code, retryable = mapped[parsed.status]
        return EngineStatus(
            run_ref=current,
            state=state,  # type: ignore[arg-type]
            stable_error_code=stable_code,
            retryable=retryable,
        )

    async def cancel(self, run_ref: EngineRunRef) -> EngineTerminalStateValue:
        response, current = await self._request_json_with_resume(
            run_ref,
            method="POST",
            path=lambda ref: self._session_path(ref, "cancel"),
            json_body={},
        )
        try:
            parsed = SmartPerfettoCancelResponse.model_validate(response.payload)
        except ValidationError:
            raise _error("engine_contract_invalid") from None
        if parsed.session_id != current.external_session_id:
            raise _error("engine_session_lost")
        return "canceled"

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult:
        response, _current = await self._request_json_with_resume(
            run_ref,
            method="GET",
            path=lambda ref: self._session_path(ref, "report"),
        )
        try:
            parsed = SmartPerfettoReportResponse.model_validate(response.payload)
        except ValidationError:
            raise _error("engine_contract_invalid") from None
        report = parsed.sanitized_report
        state: EngineTerminalStateValue = (
            "completed" if self._has_usable_result(report) else "insufficient_data"
        )
        return EngineResult(
            contract="workspace-agent-v1",
            state=state,
            payload={
                "reportId": parsed.report_id,
                "report": report,
            },
        )

    @staticmethod
    def _has_usable_result(report: dict[str, object]) -> bool:
        summary = report.get("summary")
        conclusion = summary.get("conclusion") if isinstance(summary, dict) else None
        if isinstance(conclusion, str) and conclusion.strip():
            return True
        for key in ("findings", "claimSupport", "claimVerificationResult"):
            value = report.get(key)
            if isinstance(value, list | tuple) and len(value) > 0:
                return True
            if isinstance(value, dict) and len(value) > 0:
                return True
        return False

    async def _request_json_with_resume(
        self,
        run_ref: EngineRunRef,
        *,
        method: str,
        path: Callable[[EngineRunRef], str],
        json_body: dict[str, object] | None = None,
    ) -> tuple[Any, EngineRunRef]:
        current = self._validate_run_ref(run_ref, require_run=False)
        for attempt in range(2):
            response = await self._transport.request_json(
                method,
                path(current),
                workspace_id=current.external_workspace_id,
                json_body=json_body,
            )
            if response.status_code != 404:
                if not 200 <= response.status_code <= 299:
                    raise self._endpoint_error(
                        operation="session",
                        status_code=response.status_code,
                        payload=response.payload,
                    )
                return response, current
            if attempt == 0:
                current = await self._resume(current)
        raise _error("engine_session_lost")

    async def _resume(self, run_ref: EngineRunRef) -> EngineRunRef:
        current = self._validate_run_ref(run_ref, require_run=False)
        workspace_id = current.external_workspace_id
        session_id = current.external_session_id
        assert workspace_id is not None and session_id is not None
        response = await self._transport.request_json(
            "POST",
            f"/api/workspaces/{workspace_id}/agent/resume",
            workspace_id=workspace_id,
            json_body={"sessionId": session_id},
        )
        if not 200 <= response.status_code <= 299:
            raise _error("engine_session_lost")
        try:
            resumed = SmartPerfettoResumeResponse.model_validate(response.payload)
        except ValidationError:
            raise _error("engine_session_lost") from None
        if resumed.session_id != session_id:
            raise _error("engine_session_lost")
        run_id = resumed.run_id or current.external_run_id
        if run_id is not None:
            try:
                run_id = validate_external_id(run_id)
            except EngineAdapterError:
                raise _error("engine_session_lost") from None
        return replace(current, external_run_id=run_id)

    @staticmethod
    def _validate_run_ref(
        run_ref: EngineRunRef,
        *,
        require_run: bool,
    ) -> EngineRunRef:
        if run_ref.engine_id != "smartperfetto":
            raise _error("engine_contract_invalid")
        try:
            workspace_id = validate_external_id(run_ref.external_workspace_id or "")
            session_id = validate_external_id(run_ref.external_session_id or "")
            run_id = (
                validate_external_id(run_ref.external_run_id)
                if run_ref.external_run_id is not None
                else None
            )
        except EngineAdapterError:
            raise _error("engine_contract_invalid") from None
        if require_run and run_id is None:
            raise _error("engine_contract_invalid")
        return replace(
            run_ref,
            external_workspace_id=workspace_id,
            external_session_id=session_id,
            external_run_id=run_id,
        )

    @staticmethod
    def _session_path(run_ref: EngineRunRef, operation: str) -> str:
        workspace_id = run_ref.external_workspace_id
        session_id = run_ref.external_session_id
        assert workspace_id is not None and session_id is not None
        return f"/api/workspaces/{workspace_id}/agent/{session_id}/{operation}"

    async def _submit_auto(
        self,
        *,
        trace_id: str,
        workspace_id: str,
        question: str | None,
    ) -> EngineRunRef:
        preview_ref = await self._analyze(
            workspace_id=workspace_id,
            trace_id=trace_id,
            query=_PREVIEW_QUERY,
            options={
                "analysisMode": "auto",
                "preset": "smart",
                "smartAction": "preview",
            },
        )
        try:
            preview = await self._read_preview(preview_ref)
        except asyncio.CancelledError:
            await asyncio.shield(self._best_effort_cancel(preview_ref))
            raise
        supported = {
            scene.scene_type
            for scene in preview.scenes
            if scene.scene_type in _SUPPORTED_SCENE_TYPES
        }
        if not supported:
            raise _error("unsupported_trace_profile")
        return await self._analyze(
            workspace_id=workspace_id,
            trace_id=trace_id,
            query=_append_context(_DEEP_DIVE_QUERY, question),
            options={
                "analysisMode": "auto",
                "preset": "smart",
                "smartAction": "analyze",
                "smartSelection": {
                    "scope": "scene_types",
                    "sceneTypes": list(_SUPPORTED_SCENE_TYPES),
                    "label": "PerfPilot supported scenes",
                    "reportId": preview.report_id,
                },
            },
        )

    async def _read_preview(self, run_ref: EngineRunRef) -> SmartPerfettoScenePreview:
        current = self._validate_run_ref(run_ref, require_run=True)
        cursor: str | None = None
        for attempt in range(2):
            workspace_id = current.external_workspace_id
            run_id = current.external_run_id
            assert workspace_id is not None and run_id is not None
            async with self._transport.stream_response(
                f"/api/workspaces/{workspace_id}/agent/runs/{run_id}/stream",
                workspace_id=workspace_id,
                last_event_id=cursor,
            ) as response:
                if response.status_code == 404:
                    if attempt == 1:
                        raise _error("engine_session_lost")
                    current = await self._resume(current)
                    continue
                if response.status_code != 200:
                    raise self._endpoint_error(
                        operation="preview",
                        status_code=response.status_code,
                        payload={},
                    )
                async for frame in parse_sse_frames(
                    response.aiter_bytes(),
                    max_event_bytes=self._max_sse_event_bytes,
                ):
                    if frame.event_id is not None:
                        cursor = frame.event_id
                    if frame.event == "analysis_completed":
                        return self._parse_preview(frame.data)
                    if frame.event == "analysis_cancelled":
                        raise _error("engine_unavailable")
                    if frame.event == "error":
                        raise _error(
                            "engine_unavailable",
                            retryable=True,
                            terminal_state=None,
                        )
            if attempt == 0:
                preview_status = await self.status(replace(current, cursor=cursor))
                current = preview_status.run_ref
                if preview_status.stable_error_code is not None:
                    raise _error(
                        preview_status.stable_error_code,
                        retryable=preview_status.retryable,
                        terminal_state=None if preview_status.retryable else "failed",
                    )
                if preview_status.state == "canceled":
                    raise _error("engine_session_lost")
        raise _error("engine_contract_invalid")

    @staticmethod
    def _parse_preview(raw_data: str) -> SmartPerfettoScenePreview:
        try:
            payload = json.loads(raw_data)
            if not isinstance(payload, dict):
                raise TypeError
            data = payload.get("data")
            if not isinstance(data, dict):
                raise TypeError
            preview = data.get("smartScenePreview")
            return SmartPerfettoScenePreview.model_validate(preview)
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
            raise _error("engine_contract_invalid") from None

    async def _best_effort_cancel(self, run_ref: EngineRunRef) -> None:
        workspace_id = run_ref.external_workspace_id
        session_id = run_ref.external_session_id
        if workspace_id is None or session_id is None:
            return
        try:
            response = await self._transport.request_json(
                "POST",
                f"/api/workspaces/{workspace_id}/agent/{session_id}/cancel",
                workspace_id=workspace_id,
            )
            SmartPerfettoCancelResponse.model_validate(response.payload)
        except (EngineAdapterError, ValidationError):
            return

    @staticmethod
    def _endpoint_error(
        *,
        operation: str,
        status_code: int,
        payload: dict[str, Any],
    ) -> EngineAdapterError:
        code: str | None = None
        try:
            code = SmartPerfettoEndpointError.model_validate(payload).code
        except ValidationError:
            pass
        if code in {
            "TRACE_SIZE_QUOTA_EXCEEDED",
            "WORKSPACE_TRACE_STORAGE_QUOTA_EXCEEDED",
            "MONTHLY_RUN_QUOTA_EXCEEDED",
        }:
            return _error("engine_quota_exceeded")
        if code == "TENANT_TOMBSTONED" or status_code == 423:
            return _error("engine_tenant_unavailable")
        if (
            code == "CONCURRENT_RUN_QUOTA_EXCEEDED"
            or status_code == 429
            or (operation == "analyze" and status_code == 409)
        ):
            return _error("capacity_exceeded", retryable=True, terminal_state=None)
        if code and "INVALID_TRACE" in code:
            return _error("trace_integrity_failed")
        if status_code in {401, 403}:
            return _error("engine_auth_failed")
        if 200 <= status_code <= 299 and code is None:
            return _error("engine_contract_invalid")
        return _error("engine_unavailable", retryable=True, terminal_state=None)


__all__ = ["SmartPerfettoAdapter"]
