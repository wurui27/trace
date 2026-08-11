"""Production composition for the recoverable PerfPilot synthesis worker."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from perfpilot_api.ai.openai_compatible import OpenAICompatibleSynthesisProvider
from perfpilot_api.ai.prompt import load_synthesis_prompt
from perfpilot_api.config import Settings, get_settings
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.engines.lock import load_engine_lock
from perfpilot_api.runtime.artifacts import build_artifact_runtime
from perfpilot_api.runtime.secrets import read_owner_only_file
from perfpilot_api.services.canonical_result_reader import CanonicalResultReader
from perfpilot_api.services.engine_result_artifacts import (
    SQLAlchemyEngineResultArtifactRepository,
)
from perfpilot_api.services.synthesis_artifacts import (
    S3SynthesisArtifactStore,
    SQLAlchemySynthesisArtifactRepository,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
)
from perfpilot_api.services.source_artifacts import S3SourceArtifactService
from perfpilot_api.services.source_tasks import (
    SQLAlchemySourceTaskRepository,
    SourceTaskService,
)
from perfpilot_api.services.uploads import SQLAlchemyTenantBucketResolver
from perfpilot_api.reports.writer import AnalysisReportWriter
from perfpilot_api.workers.synthesis_orchestrator import (
    SQLAlchemyAutomaticSynthesisRequestFactory,
    SQLAlchemySynthesisAnalysisContextRepository,
    SQLAlchemySynthesisMemorySourceRepository,
    SQLAlchemySynthesisParentProjector,
    SQLAlchemySynthesisWorkQueue,
    SynthesisCoordinator,
    SynthesisOrchestrationWorker,
    SynthesisPipeline,
)
from perfpilot_api.workers.source_orchestrator import (
    InMemorySourceAnalysisStateRepository,
    NoopSynthesisScheduler,
    SourceOrchestrator,
    SQLAlchemySourceAuthorityReader,
)


_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_WORKER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")
_CloseCallback = Callable[[], object]


class SynthesisWorkerRuntimeError(RuntimeError):
    def __init__(self, message: str = "synthesis worker runtime is unavailable") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SynthesisRuntimeInputs:
    worker_id: str
    report_worker_image_digest: str
    credential_path: Path
    engine_lock_path: Path
    engine_lock_schema_path: Path
    provider_host: str


def _required_environment(name: str) -> str:
    value = os.getenv(name, "")
    if not value or value != value.strip():
        raise SynthesisWorkerRuntimeError(f"{name} is required")
    return value


def _absolute_path(name: str) -> Path:
    value = Path(_required_environment(name))
    rendered = value.as_posix()
    if (
        not value.is_absolute()
        or value == Path(value.anchor)
        or rendered.startswith("//")
        or ".." in value.parts
        or any(character in rendered for character in ("\n", "\r", "\x00"))
    ):
        raise SynthesisWorkerRuntimeError(f"{name} must be an absolute path")
    return value


def validate_synthesis_runtime_environment(settings: Settings) -> SynthesisRuntimeInputs:
    if settings.app_env != "production" or not settings.ai_enabled:
        raise SynthesisWorkerRuntimeError("synthesis worker requires production AI")
    worker_id = _required_environment("PERFPILOT_SYNTHESIS_WORKER_ID")
    digest = _required_environment("PERFPILOT_REPORT_WORKER_IMAGE_DIGEST")
    if _WORKER.fullmatch(worker_id) is None or _IMAGE.fullmatch(digest) is None:
        raise SynthesisWorkerRuntimeError("synthesis worker identity is invalid")
    credential_path = _absolute_path("PERFPILOT_AI_CREDENTIAL_FILE")
    lock_path = _absolute_path("PERFPILOT_ENGINE_LOCK_FILE")
    schema_path = _absolute_path("PERFPILOT_ENGINE_LOCK_SCHEMA_FILE")
    try:
        provider_host = urlsplit(
            settings.ai_base_url.get_secret_value()
        ).hostname
    except ValueError:
        provider_host = None
    raw_allowlist = _required_environment("PERFPILOT_AI_EGRESS_ALLOWLIST")
    allowlist = {
        item.rstrip(".").casefold()
        for item in raw_allowlist.split(",")
        if item and item == item.strip() and _HOST.fullmatch(item) is not None
    }
    if (
        provider_host is None
        or provider_host.rstrip(".").casefold() not in allowlist
        or len(allowlist) != len(raw_allowlist.split(","))
    ):
        raise SynthesisWorkerRuntimeError("AI provider egress is not allowed")
    return SynthesisRuntimeInputs(
        worker_id=worker_id,
        report_worker_image_digest=digest,
        credential_path=credential_path,
        engine_lock_path=lock_path,
        engine_lock_schema_path=schema_path,
        provider_host=provider_host.rstrip(".").casefold(),
    )


def _load_token(path: Path) -> SecretStr:
    try:
        payload = read_owner_only_file(path)
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]
        token = payload.decode("utf-8")
        if (
            not 16 <= len(token) <= 4096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            raise ValueError
        return SecretStr(token)
    except Exception:
        raise SynthesisWorkerRuntimeError("AI credential is unavailable") from None


async def _close_callbacks(
    callbacks: tuple[_CloseCallback, ...],
) -> BaseException | None:
    first: BaseException | None = None
    for callback in reversed(callbacks):
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            if first is None or (
                isinstance(error, _CONTROL_FLOW_EXCEPTIONS)
                and not isinstance(first, _CONTROL_FLOW_EXCEPTIONS)
            ):
                first = error
    return first


@dataclass(slots=True)
class SynthesisWorkerRuntime:
    worker: SynthesisOrchestrationWorker
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
        raise SynthesisWorkerRuntimeError() from None


async def build_production_synthesis_worker() -> SynthesisWorkerRuntime:
    settings = get_settings()
    inputs = validate_synthesis_runtime_environment(settings)
    callbacks: list[_CloseCallback] = []
    runtime: SynthesisWorkerRuntime | None = None
    build_failure: BaseException | None = None
    try:
        load_engine_lock(
            inputs.engine_lock_path,
            schema_path=inputs.engine_lock_schema_path,
            require_image_digests=True,
        )
        token = _load_token(inputs.credential_path)
        control_engine = create_control_engine(
            settings.control_database_url.get_secret_value()
        )
        callbacks.append(control_engine.dispose)
        control_sessions = create_control_session_factory(control_engine)
        artifacts = await build_artifact_runtime(
            settings=settings,
            control_session_factory=control_sessions,
            include_local_apk_inspector=False,
        )
        callbacks.append(artifacts.close)
        bucket_resolver = SQLAlchemyTenantBucketResolver(
            session_factory=control_sessions
        )
        canonical_reader = CanonicalResultReader(
            artifact_repository=SQLAlchemyEngineResultArtifactRepository(
                tenant_router=artifacts.tenant_router
            ),
            bucket_resolver=bucket_resolver,
            client=artifacts.s3_client,
        )
        synthesis_artifacts = S3SynthesisArtifactStore(
            repository=SQLAlchemySynthesisArtifactRepository(
                tenant_router=artifacts.tenant_router
            ),
            bucket_resolver=bucket_resolver,
            client=artifacts.s3_client,
        )
        timeout = httpx.Timeout(
            timeout=settings.ai_read_timeout_seconds,
            connect=settings.ai_connect_timeout_seconds,
            read=settings.ai_read_timeout_seconds,
            write=settings.ai_write_timeout_seconds,
            pool=settings.ai_pool_timeout_seconds,
        )
        provider_client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            timeout=timeout,
        )
        callbacks.append(provider_client.aclose)
        prompt = load_synthesis_prompt()
        provider = OpenAICompatibleSynthesisProvider(
            base_url=settings.ai_base_url,
            model=settings.ai_model,
            token=token,
            prompt=prompt,
            max_response_bytes=settings.ai_max_response_bytes,
            client=provider_client,
        )
        repository = SQLAlchemySynthesisExecutionRepository(control_sessions)
        inference_payload = json.dumps(
            {
                "connect_timeout": settings.ai_connect_timeout_seconds,
                "max_projection_bytes": settings.ai_max_projection_bytes,
                "max_response_bytes": settings.ai_max_response_bytes,
                "model": settings.ai_model,
                "pool_timeout": settings.ai_pool_timeout_seconds,
                "provider": settings.ai_provider_name,
                "read_timeout": settings.ai_read_timeout_seconds,
                "write_timeout": settings.ai_write_timeout_seconds,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        source_orchestrator = SourceOrchestrator(
            authority=SQLAlchemySourceAuthorityReader(control_sessions),
            tasks=SourceTaskService(
                repository=SQLAlchemySourceTaskRepository(control_sessions)
            ),
            artifacts=S3SourceArtifactService(
                tenant_router=artifacts.tenant_router,
                bucket_resolver=bucket_resolver,
                client=artifacts.s3_client,
            ),
            states=InMemorySourceAnalysisStateRepository(),
            scheduler=NoopSynthesisScheduler(),
        )
        coordinator = SynthesisCoordinator(
            session_factory=control_sessions,
            repository=repository,
            request_factory=SQLAlchemyAutomaticSynthesisRequestFactory(
                tenant_router=artifacts.tenant_router,
                normalizer_version="smartperfetto-normalizer-1",
                prompt_template_version=prompt.version,
                prompt_template_sha256_b64=prompt.sha256_b64,
                report_worker_image_digest=inputs.report_worker_image_digest,
                provider_name=settings.ai_provider_name,
                model=settings.ai_model,
                inference_config_hash=hashlib.sha256(inference_payload).hexdigest(),
            ),
            source_gate=source_orchestrator.prepare_for_synthesis,
        )
        pipeline = SynthesisPipeline(
            repository=repository,
            canonical_reader=canonical_reader,
            artifact_store=synthesis_artifacts,
            provider=provider,
            report_writer=AnalysisReportWriter(
                tenant_router=artifacts.tenant_router
            ),
            analysis_contexts=SQLAlchemySynthesisAnalysisContextRepository(
                tenant_router=artifacts.tenant_router
            ),
            memory_sources=SQLAlchemySynthesisMemorySourceRepository(
                session_factory=control_sessions
            ),
            parent_projector=SQLAlchemySynthesisParentProjector(
                control_session_factory=control_sessions,
                tenant_router=artifacts.tenant_router,
            ),
            max_projection_bytes=settings.ai_max_projection_bytes,
        )
        worker = SynthesisOrchestrationWorker(
            coordinator=coordinator,
            queue=SQLAlchemySynthesisWorkQueue(
                control_sessions, lease_seconds=30
            ),
            pipeline=pipeline,
            worker_id=inputs.worker_id,
            heartbeat_seconds=10,
        )
        runtime = SynthesisWorkerRuntime(worker, tuple(callbacks))
    except BaseException as error:
        build_failure = error
    if build_failure is not None or runtime is None:
        cleanup_failure = await _close_callbacks(tuple(callbacks))
        if isinstance(build_failure, _CONTROL_FLOW_EXCEPTIONS):
            raise build_failure
        if isinstance(cleanup_failure, _CONTROL_FLOW_EXCEPTIONS):
            raise cleanup_failure
        raise SynthesisWorkerRuntimeError() from None
    return runtime


def main() -> None:
    async def run() -> None:
        runtime = await build_production_synthesis_worker()
        try:
            await runtime.run_forever()
        finally:
            await runtime.close()

    asyncio.run(run())


__all__ = [
    "SynthesisRuntimeInputs",
    "SynthesisWorkerRuntime",
    "SynthesisWorkerRuntimeError",
    "build_production_synthesis_worker",
    "main",
    "validate_synthesis_runtime_environment",
]
