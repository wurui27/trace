import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

from perfpilot_api.config import Settings
from perfpilot_api.db.tenant.router import (
    SqlAlchemyTenantRouteRepository,
    TenantClusterEndpoint,
    TenantRouter,
)
from perfpilot_api.runtime.secrets import build_configured_secret_store
from perfpilot_api.services import uploads as upload_core
from perfpilot_api.services.apk_inspection import (
    S3ApkInspector,
    S3VersionedObjectReader,
    SQLAlchemyApkArtifactLocator,
)
from perfpilot_api.storage.s3 import S3ArtifactStore


_CONTROL_FLOW_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


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


class ArtifactRuntimeError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("artifact runtime is unavailable")


def create_s3_client(
    *,
    settings: Settings,
    client_factory: Any | None = None,
) -> Any:
    endpoint_url = str(settings.s3_endpoint_url).rstrip("/")
    parsed_endpoint = urlsplit(endpoint_url)
    if (
        parsed_endpoint.scheme != "https"
        or parsed_endpoint.hostname is None
        or not settings.s3_region
    ):
        raise ArtifactRuntimeError
    factory = client_factory or boto3.client
    failed = False
    client: Any = None
    try:
        client = factory(
            "s3",
            endpoint_url=endpoint_url,
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )
    except Exception:
        failed = True
    if failed or client is None:
        raise ArtifactRuntimeError
    return client


async def _close_owned_components(
    *,
    tenant_router: Any | None,
    s3_client: Any | None,
    secret_store: Any | None,
) -> BaseException | None:
    first_error: BaseException | None = None
    if tenant_router is not None:
        try:
            await tenant_router.dispose()
        except BaseException as error:
            first_error = _prefer_control_flow_error(first_error, error)
    if s3_client is not None:
        try:
            await asyncio.to_thread(s3_client.close)
        except BaseException as error:
            first_error = _prefer_control_flow_error(first_error, error)
    if secret_store is not None:
        try:
            await asyncio.to_thread(secret_store.close)
        except BaseException as error:
            first_error = _prefer_control_flow_error(first_error, error)
    return first_error


@dataclass(slots=True)
class ArtifactRuntime:
    upload_service: Any
    apk_inspector: Any
    tenant_router: Any = field(repr=False)
    s3_client: Any = field(repr=False)
    secret_store: Any = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        if self._closed:
            return
        failure = await _close_owned_components(
            tenant_router=self.tenant_router,
            s3_client=self.s3_client,
            secret_store=self.secret_store,
        )
        if failure is None:
            self._closed = True
            return
        if isinstance(failure, _CONTROL_FLOW_EXCEPTIONS):
            raise failure
        raise ArtifactRuntimeError from None


async def build_artifact_runtime(
    *,
    settings: Settings,
    control_session_factory: Any,
) -> ArtifactRuntime:
    secret_store: Any | None = None
    tenant_router: Any | None = None
    s3_client: Any | None = None
    runtime: ArtifactRuntime | None = None
    build_failure: BaseException | None = None
    try:
        secret_store = build_configured_secret_store(
            keyring_config=settings.secret_keyring_config,
            secret_store_root=settings.secret_store_root,
        )
        route_repository = SqlAlchemyTenantRouteRepository(session_factory=control_session_factory)
        tenant_router = TenantRouter(
            control_resources=route_repository,
            secret_store=secret_store,
            cluster=TenantClusterEndpoint(
                host=settings.tenant_cluster_host,
                port=settings.tenant_cluster_port,
                sslmode=settings.tenant_cluster_sslmode,
            ),
        )
        bucket_resolver_factory = getattr(
            upload_core,
            "SQLAlchemyTenantBucketResolver",
        )
        upload_repository_factory = getattr(
            upload_core,
            "SQLAlchemyUploadRepository",
        )
        bucket_resolver = bucket_resolver_factory(session_factory=control_session_factory)
        upload_repository = upload_repository_factory(tenant_router=tenant_router)
        s3_client = create_s3_client(settings=settings)
        artifact_store = S3ArtifactStore(client=s3_client)
        upload_service = upload_core.UploadService(
            repository=upload_repository,
            artifact_store=artifact_store,
            bucket_resolver=bucket_resolver,
        )
        apk_locator = SQLAlchemyApkArtifactLocator(
            tenant_router=tenant_router,
            bucket_resolver=bucket_resolver,
        )
        object_reader = S3VersionedObjectReader(client=s3_client)
        apk_inspector = S3ApkInspector(
            locator=apk_locator,
            object_reader=object_reader,
            apkanalyzer_binary=str(settings.apkanalyzer_binary),
        )
        runtime = ArtifactRuntime(
            upload_service=upload_service,
            apk_inspector=apk_inspector,
            tenant_router=tenant_router,
            s3_client=s3_client,
            secret_store=secret_store,
        )
    except BaseException as error:
        build_failure = error
    if build_failure is not None or runtime is None:
        cleanup_failure = await _close_owned_components(
            tenant_router=tenant_router,
            s3_client=s3_client,
            secret_store=secret_store,
        )
        if isinstance(build_failure, _CONTROL_FLOW_EXCEPTIONS):
            raise build_failure
        if isinstance(cleanup_failure, _CONTROL_FLOW_EXCEPTIONS):
            raise cleanup_failure
        raise ArtifactRuntimeError from None
    return runtime


__all__ = [
    "ArtifactRuntime",
    "ArtifactRuntimeError",
    "build_artifact_runtime",
    "create_s3_client",
]
