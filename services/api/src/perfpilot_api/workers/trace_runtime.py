"""Production composition for the durable SmartPerfetto Trace worker."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import SecretStr

from perfpilot_api.config import get_settings
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.engines.lock import load_engine_lock
from perfpilot_api.runtime.artifacts import build_artifact_runtime
from perfpilot_api.runtime.secrets import read_owner_only_file
from perfpilot_api.services.engine_executions import (
    build_smartperfetto_execution_service,
)
from perfpilot_api.services.trace_executions import (
    SQLAlchemyTraceExecutionRepository,
    TraceExecutionService,
)
from perfpilot_api.workers.trace_orchestrator import (
    SQLAlchemyTraceWorkQueueRepository,
    TraceOrchestrationWorker,
)


_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_CloseCallback = Callable[[], object]


class TraceWorkerRuntimeError(RuntimeError):
    """A secret-safe Trace worker composition or cleanup failure."""


class MountedSmartPerfettoCredentialResolver:
    """Resolve one bound service reference from a rotatable owner-only file."""

    __slots__ = ("_expected_digest", "_path")

    def __init__(
        self,
        *,
        expected_reference: SecretStr,
        path: Path,
    ) -> None:
        reference = expected_reference.get_secret_value()
        if (
            not reference
            or reference != reference.strip()
            or len(reference) > 512
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in reference)
            or not _is_unambiguous_absolute_path(path)
        ):
            raise TraceWorkerRuntimeError(
                "SmartPerfetto credential is unavailable"
            ) from None
        self._expected_digest = hashlib.sha256(reference.encode("utf-8")).digest()
        self._path = path
        self._load_token()

    def _load_token(self) -> str:
        try:
            payload = read_owner_only_file(self._path)
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            elif payload.endswith(b"\n"):
                payload = payload[:-1]
            token = payload.decode("utf-8")
            if (
                not 16 <= len(token) <= 4_096
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
            ):
                raise ValueError
            return token
        except Exception:
            raise TraceWorkerRuntimeError(
                "SmartPerfetto credential is unavailable"
            ) from None

    async def __call__(self, reference: SecretStr) -> SecretStr:
        try:
            reference_digest = hashlib.sha256(
                reference.get_secret_value().encode("utf-8")
            ).digest()
        except Exception:
            raise TraceWorkerRuntimeError(
                "SmartPerfetto credential is unavailable"
            ) from None
        if not hmac.compare_digest(reference_digest, self._expected_digest):
            raise TraceWorkerRuntimeError(
                "SmartPerfetto credential is unavailable"
            ) from None
        token = await asyncio.to_thread(self._load_token)
        return SecretStr(token)


async def _close_callbacks(
    callbacks: tuple[_CloseCallback, ...],
) -> BaseException | None:
    first_error: BaseException | None = None
    for callback in reversed(callbacks):
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            if first_error is None or (
                isinstance(error, _CONTROL_FLOW_EXCEPTIONS)
                and not isinstance(first_error, _CONTROL_FLOW_EXCEPTIONS)
            ):
                first_error = error
    return first_error


@dataclass(slots=True)
class TraceWorkerRuntime:
    worker: TraceOrchestrationWorker
    close_callbacks: tuple[_CloseCallback, ...] = field(default=(), repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def run_once(self) -> bool:
        return await self.worker.run_once()

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        await self.worker.run_forever(stop)

    async def close(self) -> None:
        if self._closed:
            return
        failure = await _close_callbacks(self.close_callbacks)
        if failure is None:
            self._closed = True
            return
        if isinstance(failure, _CONTROL_FLOW_EXCEPTIONS):
            raise failure
        raise TraceWorkerRuntimeError("trace worker cleanup failed") from None


def _is_unambiguous_absolute_path(path: Path) -> bool:
    rendered = path.as_posix()
    return (
        path.is_absolute()
        and path != Path(path.anchor)
        and not rendered.startswith("//")
        and ".." not in path.parts
        and "\n" not in rendered
        and "\r" not in rendered
        and "\x00" not in rendered
    )


def _required_environment(name: str) -> str:
    value = os.getenv(name, "")
    if not value or value != value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _required_path(name: str) -> Path:
    path = Path(_required_environment(name))
    if not _is_unambiguous_absolute_path(path):
        raise RuntimeError(f"{name} must be an absolute path")
    return path


async def build_production_trace_worker() -> TraceWorkerRuntime:
    """Build only the production, pinned and externally authenticated runtime."""

    settings = get_settings()
    if settings.app_env != "production":
        raise RuntimeError("trace worker requires a production environment")
    if not settings.smartperfetto_enabled:
        raise RuntimeError("SmartPerfetto must be enabled for the trace worker")

    worker_id = _required_environment("PERFPILOT_TRACE_WORKER_ID")
    credential_path = _required_path("PERFPILOT_SMARTPERFETTO_CREDENTIAL_FILE")
    lock_path = _required_path("PERFPILOT_ENGINE_LOCK_FILE")
    schema_path = _required_path("PERFPILOT_ENGINE_LOCK_SCHEMA_FILE")

    callbacks: list[_CloseCallback] = []
    runtime: TraceWorkerRuntime | None = None
    build_failure: BaseException | None = None
    try:
        engine_lock = load_engine_lock(
            lock_path,
            schema_path=schema_path,
            require_image_digests=True,
        )
        credential_resolver = MountedSmartPerfettoCredentialResolver(
            expected_reference=settings.smartperfetto_credential_reference,
            path=credential_path,
        )
        control_engine = create_control_engine(
            settings.control_database_url.get_secret_value()
        )
        callbacks.append(control_engine.dispose)
        control_sessions = create_control_session_factory(control_engine)

        artifact_runtime = await build_artifact_runtime(
            settings=settings,
            control_session_factory=control_sessions,
            include_local_apk_inspector=False,
        )
        callbacks.append(artifact_runtime.close)

        timeout = httpx.Timeout(
            timeout=settings.smartperfetto_read_timeout_seconds,
            connect=settings.smartperfetto_connect_timeout_seconds,
            read=settings.smartperfetto_read_timeout_seconds,
            write=settings.smartperfetto_write_timeout_seconds,
            pool=settings.smartperfetto_pool_timeout_seconds,
        )
        engine_client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
        )
        callbacks.append(engine_client.aclose)
        artifact_client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
        )
        callbacks.append(artifact_client.aclose)

        engine_service = build_smartperfetto_execution_service(
            settings=settings,
            control_session_factory=control_sessions,
            credential_resolver=credential_resolver,
            engine_client=engine_client,
            artifact_client=artifact_client,
            engine_lock=engine_lock,
            result_sink=artifact_runtime.engine_result_sink,
        )
        execution_service = TraceExecutionService(
            repository=SQLAlchemyTraceExecutionRepository(
                control_session_factory=control_sessions,
                tenant_router=artifact_runtime.tenant_router,
            ),
            upload_service=artifact_runtime.upload_service,
            engine_service=engine_service,
        )
        queue = SQLAlchemyTraceWorkQueueRepository(
            session_factory=control_sessions,
            lease_seconds=30,
        )
        worker = TraceOrchestrationWorker(
            queue=queue,
            service=execution_service,
            worker_id=worker_id,
            idle_poll_seconds=1,
            active_poll_seconds=2,
            failure_backoff_seconds=5,
            heartbeat_seconds=10,
        )
        runtime = TraceWorkerRuntime(
            worker=worker,
            close_callbacks=tuple(callbacks),
        )
    except BaseException as error:
        build_failure = error

    if build_failure is not None or runtime is None:
        cleanup_failure = await _close_callbacks(tuple(callbacks))
        if isinstance(build_failure, _CONTROL_FLOW_EXCEPTIONS):
            raise build_failure
        if isinstance(cleanup_failure, _CONTROL_FLOW_EXCEPTIONS):
            raise cleanup_failure
        raise TraceWorkerRuntimeError("trace worker runtime is unavailable") from None
    return runtime


def main() -> None:
    async def run() -> None:
        runtime = await build_production_trace_worker()
        try:
            await runtime.run_forever()
        finally:
            await runtime.close()

    asyncio.run(run())


__all__ = [
    "MountedSmartPerfettoCredentialResolver",
    "TraceWorkerRuntime",
    "TraceWorkerRuntimeError",
    "build_production_trace_worker",
    "main",
]
