import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send
from redis import asyncio as redis

from perfpilot_api.api.auth import PROXY_CLIENT_IDENTITY_STATE_KEY
from perfpilot_api.api.auth import ProxyAuthenticationMiddleware
from perfpilot_api.api.auth import router as auth_router
from perfpilot_api.api.agent_control import router as agent_control_router
from perfpilot_api.api.agents import router as agents_router
from perfpilot_api.api.admin_teams import router as admin_teams_router
from perfpilot_api.api.analyses import router as analyses_router
from perfpilot_api.api.health import router as health_router
from perfpilot_api.api.me import router as me_router
from perfpilot_api.api.memory_captures import router as memory_captures_router
from perfpilot_api.api.members import router as members_router
from perfpilot_api.api.uploads import router as uploads_router
from perfpilot_api.ai.prompt import load_synthesis_prompt
from perfpilot_api.config import Settings, get_settings
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    internal_server_error_handler,
    request_validation_error_handler,
)
from perfpilot_api.engines.android_memory import AndroidMemoryAdapter
from perfpilot_api.engines.android_memory_stager import AndroidMemoryStager
from perfpilot_api.engines.android_memory_worker import (
    AndroidMemoryWorker,
    LocalAndroidMemoryWorker,
    OciAndroidMemoryWorker,
)
from perfpilot_api.engines.registry import AdapterRegistry
from perfpilot_api.security.proxy_signature import (
    InMemoryReplayStore,
    RedisReplayStore,
    ReplayStore,
)
from perfpilot_api.security.agent_credentials import AgentCredentialCodec
from perfpilot_api.security.agent_signatures import (
    RedisAgentNonceStore,
    encode_ed25519_public_key,
)
from perfpilot_api.security.task_snapshots import SourceTaskSnapshotSigner, TaskSnapshotSigner
from perfpilot_api.runtime.artifacts import ArtifactRuntime, build_artifact_runtime
from perfpilot_api.services.auth import (
    AuthService,
    RedisLoginRateLimiter,
    RedisPreAuthSessionLimiter,
)
from perfpilot_api.services.agents import (
    AgentService,
    SQLAlchemyAgentRepository,
    TaskSigningKey,
)
from perfpilot_api.services.device_directory import (
    DeviceDirectory,
    SQLAlchemyDeviceDirectoryRepository,
)
from perfpilot_api.services.agent_tasks import (
    AgentTaskService,
    RedisAgentTaskWakeup,
    SQLAlchemyAgentTaskRepository,
)
from perfpilot_api.services.agent_uploads import (
    AgentUploadService,
    SQLAlchemyAgentUploadRepository,
)
from perfpilot_api.services.source_tasks import (
    SQLAlchemySourceTaskRepository,
    SourceTaskCompletionRecorder,
    SourceTaskService,
)
from perfpilot_api.services.analyses import (
    AnalysisService,
    ApkInspector,
    SQLAlchemyAnalysisRepository,
    SynthesisRunConfiguration,
    SynthesisRunService,
)
from perfpilot_api.services.synthesis_executions import (
    SQLAlchemySynthesisExecutionRepository,
)
from perfpilot_api.services.provisioning import AdminTeamService
from perfpilot_api.services.internal_artifacts import (
    S3InternalArtifactSink,
    SQLAlchemyInternalArtifactRepository,
)
from perfpilot_api.services.memory_analyses import (
    MemoryCaptureService,
    SQLAlchemyMemoryCaptureRepository,
)
from perfpilot_api.services.uploads import SQLAlchemyTenantBucketResolver, UploadService
from perfpilot_api.services.source_workspaces import SourceWorkspaceService

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_ANDROID_MEMORY_PINNED_COMMIT = "d5514972ced78c3faa7fc17589c1ea9231645056"


def _prefer_control_flow_error(
    first_error: BaseException | None,
    error: BaseException,
) -> BaseException:
    if first_error is None or (
        isinstance(error, _CONTROL_FLOW_EXCEPTIONS)
        and not isinstance(first_error, _CONTROL_FLOW_EXCEPTIONS)
    ):
        return error
    return first_error


def _transport_client_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _verified_client_identity(request: Request) -> str:
    identity = request.scope.get("state", {}).get(PROXY_CLIENT_IDENTITY_STATE_KEY)
    if not isinstance(identity, str) or not identity:
        raise RuntimeError("verified proxy client identity is missing")
    return identity


def _derive_agent_runtime_key(
    root_secret: bytes,
    *,
    purpose: bytes,
    key_reference: str,
) -> bytes:
    return hmac.new(
        root_secret,
        purpose + b"\x00" + key_reference.encode("utf-8"),
        hashlib.sha256,
    ).digest()


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming_request_id = Headers(scope=scope).get("x-request-id")
        request_id = (
            incoming_request_id
            if incoming_request_id and _REQUEST_ID_PATTERN.fullmatch(incoming_request_id)
            else uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class AuthNoStoreMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/v1/auth"
            or path.startswith("/v1/auth/")
            or path == "/v1/me"
            or path == "/v1/admin"
            or path.startswith("/v1/admin/")
            or path == "/v1/teams"
            or path.startswith("/v1/teams/")
            or path == "/v1/agent"
            or path.startswith("/v1/agent/")
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_no_store)


def create_app(
    testing: bool = False,
    *,
    settings_override: Settings | None = None,
    auth_service: AuthService | None = None,
    admin_team_service: AdminTeamService | None = None,
    upload_service: UploadService | None = None,
    analysis_service: AnalysisService | None = None,
    synthesis_run_service: SynthesisRunService | None = None,
    memory_capture_service: MemoryCaptureService | None = None,
    agent_service: AgentService | None = None,
    device_directory: DeviceDirectory | None = None,
    source_workspace_service: SourceWorkspaceService | None = None,
    agent_task_service: AgentTaskService | None = None,
    source_task_service: SourceTaskService | None = None,
    source_task_completion_recorder: SourceTaskCompletionRecorder | None = None,
    agent_upload_service: AgentUploadService | None = None,
    android_memory_worker: AndroidMemoryWorker | None = None,
    apk_inspector: ApkInspector | None = None,
    replay_store: ReplayStore | None = None,
    proxy_clock: Callable[[], float] = time.time,
    client_address_resolver: Callable[[Request], str] | None = None,
    proxy_client_identity_required: bool | None = None,
) -> FastAPI:
    settings = settings_override or (
        Settings(
            app_env="test",
            _env_prefix="PERFPILOT_TEST_ISOLATED_",
            _env_file=None,
            _secrets_dir=None,
        )
        if testing
        else get_settings()
    )

    owned_engine = None
    owned_redis = None
    owned_artifact_runtime: ArtifactRuntime | None = None
    control_session_factory = None
    resolved_auth_service = auth_service
    resolved_admin_team_service = admin_team_service
    resolved_upload_service = upload_service
    resolved_analysis_service = analysis_service
    resolved_synthesis_run_service = synthesis_run_service
    resolved_memory_capture_service = memory_capture_service
    resolved_agent_service = agent_service
    resolved_device_directory = device_directory
    resolved_source_workspace_service = source_workspace_service
    resolved_agent_task_service = agent_task_service
    resolved_source_task_service = source_task_service
    resolved_agent_upload_service = agent_upload_service
    resolved_task_snapshot_signer: TaskSnapshotSigner | None = None
    resolved_source_task_signer: SourceTaskSnapshotSigner | None = None
    resolved_android_memory_worker = android_memory_worker
    active_android_memory_worker: AndroidMemoryWorker | None = None
    owned_android_memory_artifact_client: httpx.AsyncClient | None = None
    engine_adapter_registry = AdapterRegistry(())
    resolved_replay_store = replay_store
    artifact_runtime_required = (
        not testing
        and settings.app_env == "production"
        and (
            resolved_upload_service is None
            or resolved_analysis_service is None
            or resolved_memory_capture_service is None
        )
    )
    if not testing and (resolved_auth_service is None or resolved_replay_store is None):
        owned_redis = redis.from_url(settings.redis_url.get_secret_value())
        if resolved_replay_store is None:
            resolved_replay_store = RedisReplayStore(owned_redis)
    if not testing and (
        resolved_auth_service is None
        or resolved_admin_team_service is None
        or artifact_runtime_required
    ):
        owned_engine = create_control_engine(settings.control_database_url.get_secret_value())
        control_session_factory = create_control_session_factory(owned_engine)
        if resolved_auth_service is None:
            if owned_redis is None:
                raise RuntimeError("Redis authentication dependencies are unavailable")
            resolved_auth_service = AuthService(
                session_factory=control_session_factory,
                rate_limiter=RedisLoginRateLimiter(
                    owned_redis,
                    key_secret=settings.session_secret.get_secret_value().encode(),
                    nonce_source=lambda: secrets.token_hex(16),
                ),
                pre_auth_session_limiter=RedisPreAuthSessionLimiter(
                    owned_redis,
                    key_secret=settings.session_secret.get_secret_value().encode(),
                    nonce_source=lambda: secrets.token_hex(16),
                ),
            )
        if resolved_admin_team_service is None:
            resolved_admin_team_service = AdminTeamService(
                session_factory=control_session_factory,
            )
    if (
        not testing
        and resolved_agent_service is None
        and control_session_factory is not None
        and owned_redis is not None
    ):
        root_secret = settings.session_secret.get_secret_value().encode("utf-8")
        credential_reference = settings.agent_registration_secret_reference.get_secret_value()
        signing_reference = settings.jws_signing_key_reference.get_secret_value()
        credential_secret = _derive_agent_runtime_key(
            root_secret,
            purpose=b"perfpilot-agent-credentials-v1",
            key_reference=credential_reference,
        )
        nonce_secret = _derive_agent_runtime_key(
            root_secret,
            purpose=b"perfpilot-agent-refresh-nonce-v1",
            key_reference=credential_reference,
        )
        task_private_key = Ed25519PrivateKey.from_private_bytes(
            _derive_agent_runtime_key(
                root_secret,
                purpose=b"perfpilot-agent-task-signing-v1",
                key_reference=signing_reference,
            )
        )
        task_public_key_b64 = encode_ed25519_public_key(task_private_key.public_key())
        task_signing_kid = (
            f"lan-{hashlib.sha256(task_public_key_b64.encode('ascii')).hexdigest()[:16]}"
        )
        resolved_task_snapshot_signer = TaskSnapshotSigner(
            private_key=task_private_key,
            kid=task_signing_kid,
        )
        resolved_source_task_signer = SourceTaskSnapshotSigner(
            private_key=task_private_key,
            kid=task_signing_kid,
        )
        resolved_agent_service = AgentService(
            repository=SQLAlchemyAgentRepository(control_session_factory),
            credentials=AgentCredentialCodec(credential_secret),
            nonce_store=RedisAgentNonceStore(
                owned_redis,
                key_secret=nonce_secret,
            ),
            task_signing_key=TaskSigningKey(
                kid=task_signing_kid,
                public_key_b64=task_public_key_b64,
            ),
        )
    if not testing and resolved_device_directory is None and control_session_factory is not None:
        root_secret = settings.session_secret.get_secret_value().encode("utf-8")
        credential_reference = settings.agent_registration_secret_reference.get_secret_value()
        resolved_device_directory = DeviceDirectory(
            repository=SQLAlchemyDeviceDirectoryRepository(control_session_factory),
            serial_hmac_key=_derive_agent_runtime_key(
                root_secret,
                purpose=b"perfpilot-device-serial-v1",
                key_reference=credential_reference,
            ),
        )
    if resolved_source_workspace_service is None:
        resolved_source_workspace_service = SourceWorkspaceService(
            repository=resolved_device_directory,
            enabled=settings.source_code_analysis_enabled,
        )

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        nonlocal owned_artifact_runtime
        nonlocal resolved_analysis_service
        nonlocal resolved_synthesis_run_service
        nonlocal resolved_android_memory_worker
        nonlocal resolved_memory_capture_service
        nonlocal resolved_upload_service
        nonlocal resolved_agent_task_service
        nonlocal resolved_source_task_service
        nonlocal resolved_agent_upload_service
        nonlocal active_android_memory_worker
        nonlocal engine_adapter_registry
        nonlocal owned_android_memory_artifact_client
        lifespan_error: BaseException | None = None
        try:
            if settings.android_memory_enabled:
                if settings.app_env == "production" and (
                    resolved_android_memory_worker is None
                    or getattr(resolved_android_memory_worker, "isolation", None) != "oci"
                    or settings.android_memory_image_reference is None
                    or getattr(resolved_android_memory_worker, "image_reference", None)
                    != settings.android_memory_image_reference
                ):
                    raise RuntimeError(
                        "An externally isolated Android Memory worker is unavailable"
                    )
                if resolved_android_memory_worker is None:
                    if settings.android_memory_backend == "local":
                        resolved_android_memory_worker = LocalAndroidMemoryWorker(
                            python_binary=settings.android_memory_python_binary,
                            repository_root=settings.android_memory_checkout_root,
                            run_root=settings.android_memory_run_root / "worker",
                            runtime_commit=_ANDROID_MEMORY_PINNED_COMMIT,
                            max_output_bytes=settings.android_memory_max_output_bytes,
                        )
                    else:
                        image_reference = settings.android_memory_image_reference
                        if image_reference is None:
                            raise RuntimeError(
                                "An externally isolated Android Memory worker is unavailable"
                            )
                        resolved_android_memory_worker = OciAndroidMemoryWorker(
                            container_runtime=settings.android_memory_container_runtime,
                            image_reference=image_reference,
                            run_root=settings.android_memory_run_root / "worker",
                            max_output_bytes=settings.android_memory_max_output_bytes,
                            pids_limit=settings.android_memory_pids_limit,
                            memory_bytes=settings.android_memory_memory_bytes,
                            cpu_limit=settings.android_memory_cpu_limit,
                            tmpfs_bytes=settings.android_memory_tmpfs_bytes,
                        )
                active_android_memory_worker = resolved_android_memory_worker
                owned_android_memory_artifact_client = httpx.AsyncClient(follow_redirects=False)
                adapter = AndroidMemoryAdapter(
                    stager=AndroidMemoryStager(
                        client=owned_android_memory_artifact_client,
                        workspace_root=settings.android_memory_run_root / "staging",
                        max_files=settings.android_memory_max_files,
                        max_file_bytes=settings.android_memory_max_file_bytes,
                        max_total_bytes=settings.android_memory_max_total_bytes,
                    ),
                    worker=active_android_memory_worker,
                    max_timeout_seconds=settings.android_memory_timeout_seconds,
                )
                engine_adapter_registry = AdapterRegistry((adapter,))
                lifespan_app.state.engine_adapter_registry = engine_adapter_registry
                lifespan_app.state.android_memory_worker = active_android_memory_worker
                lifespan_app.state.android_memory_artifact_client = (
                    owned_android_memory_artifact_client
                )
                image_reference = settings.android_memory_image_reference
                lifespan_app.state.android_memory_image_digest = (
                    None if image_reference is None else image_reference.rpartition("@")[2]
                )
            if (
                resolved_source_task_service is None
                and control_session_factory is not None
                and resolved_source_task_signer is not None
            ):
                resolved_source_task_service = SourceTaskService(
                    repository=SQLAlchemySourceTaskRepository(control_session_factory),
                    signer=resolved_source_task_signer,
                )
                lifespan_app.state.source_task_service = resolved_source_task_service
            if settings.app_env == "production" and apk_inspector is None:
                raise RuntimeError("An externally isolated APK inspector is unavailable")
            if artifact_runtime_required:
                if control_session_factory is None:
                    raise RuntimeError("Artifact runtime dependencies are unavailable")
                owned_artifact_runtime = await build_artifact_runtime(
                    settings=settings,
                    control_session_factory=control_session_factory,
                    include_local_apk_inspector=settings.app_env != "production",
                )
                if resolved_upload_service is None:
                    resolved_upload_service = owned_artifact_runtime.upload_service
                lifespan_app.state.upload_service = resolved_upload_service
                if (
                    resolved_agent_task_service is None
                    and resolved_task_snapshot_signer is not None
                    and owned_redis is not None
                ):
                    resolved_agent_task_service = AgentTaskService(
                        repository=SQLAlchemyAgentTaskRepository(
                            control_session_factory=control_session_factory,
                            tenant_router=owned_artifact_runtime.tenant_router,
                        ),
                        signer=resolved_task_snapshot_signer,
                        wakeup=RedisAgentTaskWakeup(owned_redis),
                    )
                    lifespan_app.state.agent_task_service = resolved_agent_task_service
                if resolved_analysis_service is None:
                    resolved_inspector = apk_inspector
                    if settings.app_env != "production":
                        resolved_inspector = (
                            resolved_inspector or owned_artifact_runtime.apk_inspector
                        )
                    if resolved_inspector is None:
                        raise RuntimeError("An externally isolated APK inspector is unavailable")
                    resolved_analysis_service = AnalysisService(
                        repository=SQLAlchemyAnalysisRepository(
                            control_session_factory=control_session_factory,
                            tenant_router=owned_artifact_runtime.tenant_router,
                        ),
                        upload_service=resolved_upload_service,
                        apk_inspector=resolved_inspector,
                        cancellation_coordinator=resolved_agent_task_service,
                    )
                    lifespan_app.state.analysis_service = resolved_analysis_service
                if (
                    resolved_agent_upload_service is None
                    and resolved_agent_task_service is not None
                ):
                    if (
                        owned_artifact_runtime.artifact_store is None
                        or owned_artifact_runtime.bucket_resolver is None
                    ):
                        raise RuntimeError("Agent upload dependencies are unavailable")
                    resolved_agent_upload_service = AgentUploadService(
                        repository=SQLAlchemyAgentUploadRepository(
                            tenant_router=owned_artifact_runtime.tenant_router,
                        ),
                        artifact_store=owned_artifact_runtime.artifact_store,
                        bucket_resolver=owned_artifact_runtime.bucket_resolver,
                        execution_authorizer=resolved_agent_task_service,
                    )
                    lifespan_app.state.agent_upload_service = resolved_agent_upload_service
                if resolved_synthesis_run_service is None and settings.ai_enabled:
                    raw_digest = os.getenv("PERFPILOT_REPORT_WORKER_IMAGE_DIGEST", "")
                    if re.fullmatch(r"sha256:[a-f0-9]{64}", raw_digest) is None:
                        raise RuntimeError("AI synthesis runtime metadata is unavailable")
                    prompt = load_synthesis_prompt()
                    inference_payload = json.dumps(
                        {
                            "connect_timeout": settings.ai_connect_timeout_seconds,
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
                    resolved_synthesis_run_service = SynthesisRunService(
                        control_session_factory=control_session_factory,
                        tenant_router=owned_artifact_runtime.tenant_router,
                        execution_repository=SQLAlchemySynthesisExecutionRepository(
                            control_session_factory
                        ),
                        configuration=SynthesisRunConfiguration(
                            normalizer_version="smartperfetto-normalizer-1",
                            prompt_template_version=prompt.version,
                            prompt_template_sha256_b64=prompt.sha256_b64,
                            report_worker_image_digest=raw_digest,
                            provider_name=settings.ai_provider_name,
                            model=settings.ai_model,
                            inference_config_hash=hashlib.sha256(inference_payload).hexdigest(),
                        ),
                    )
                    lifespan_app.state.synthesis_run_service = resolved_synthesis_run_service
                if resolved_memory_capture_service is None:
                    resolved_memory_capture_service = MemoryCaptureService(
                        repository=SQLAlchemyMemoryCaptureRepository(
                            tenant_router=owned_artifact_runtime.tenant_router,
                        ),
                        manifest_sink=S3InternalArtifactSink(
                            repository=SQLAlchemyInternalArtifactRepository(
                                tenant_router=owned_artifact_runtime.tenant_router,
                            ),
                            bucket_resolver=SQLAlchemyTenantBucketResolver(
                                session_factory=control_session_factory,
                            ),
                            client=owned_artifact_runtime.s3_client,
                        ),
                    )
                    lifespan_app.state.memory_capture_service = resolved_memory_capture_service
            yield
        except BaseException as error:
            lifespan_error = error
        finally:
            cleanup_error: BaseException | None = None
            cleanup_steps = (
                active_android_memory_worker.shutdown
                if active_android_memory_worker is not None
                else None,
                owned_android_memory_artifact_client.aclose
                if owned_android_memory_artifact_client is not None
                else None,
                owned_artifact_runtime.close if owned_artifact_runtime is not None else None,
                owned_redis.aclose if owned_redis is not None else None,
                owned_engine.dispose if owned_engine is not None else None,
            )
            for cleanup in cleanup_steps:
                if cleanup is None:
                    continue
                try:
                    await cleanup()
                except BaseException as error:
                    cleanup_error = _prefer_control_flow_error(cleanup_error, error)
            failure = lifespan_error
            if cleanup_error is not None:
                failure = _prefer_control_flow_error(failure, cleanup_error)
            if failure is not None:
                if failure is lifespan_error or isinstance(failure, _CONTROL_FLOW_EXCEPTIONS):
                    raise failure
                raise RuntimeError("Application dependency cleanup failed") from None

    app = FastAPI(lifespan=lifespan)
    app.state.testing = testing
    app.state.settings = settings
    app.state.auth_service = resolved_auth_service
    app.state.admin_team_service = resolved_admin_team_service
    app.state.upload_service = resolved_upload_service
    app.state.analysis_service = resolved_analysis_service
    app.state.synthesis_run_service = resolved_synthesis_run_service
    app.state.memory_capture_service = resolved_memory_capture_service
    app.state.agent_service = resolved_agent_service
    app.state.device_directory = resolved_device_directory
    app.state.source_workspace_service = resolved_source_workspace_service
    app.state.agent_task_service = resolved_agent_task_service
    app.state.source_task_service = resolved_source_task_service
    app.state.source_task_completion_recorder = source_task_completion_recorder
    app.state.agent_upload_service = resolved_agent_upload_service
    app.state.engine_adapter_registry = engine_adapter_registry
    app.state.android_memory_worker = None
    app.state.android_memory_artifact_client = None
    app.state.android_memory_image_digest = None
    app.state.proxy_replay_store = resolved_replay_store or InMemoryReplayStore(clock=proxy_clock)
    app.state.proxy_clock = proxy_clock
    identity_required = (
        not testing if proxy_client_identity_required is None else proxy_client_identity_required
    )
    app.state.proxy_client_identity_required = identity_required
    app.state.client_address_resolver = client_address_resolver or (
        _verified_client_identity if identity_required else _transport_client_address
    )
    app.add_middleware(ProxyAuthenticationMiddleware)
    app.add_middleware(AuthNoStoreMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        request_validation_error_handler,
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, internal_server_error_handler)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(agent_control_router)
    app.include_router(agents_router)
    app.include_router(admin_teams_router)
    app.include_router(me_router)
    app.include_router(members_router)
    app.include_router(analyses_router)
    app.include_router(uploads_router)
    app.include_router(memory_captures_router)
    return app


def run() -> None:
    uvicorn.run("perfpilot_api.main:create_app", factory=True)
