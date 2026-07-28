from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

import boto3
from psycopg.conninfo import conninfo_to_dict

from perfpilot_api.config import get_settings
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.db.tenant.router import (
    SqlAlchemyTenantRouteRepository,
    TenantClusterEndpoint,
    TenantRouter,
)
from perfpilot_api.secrets.encrypted_file import EncryptedFileSecretStore
from perfpilot_api.services.provisioning import (
    AlembicTenantMigrator,
    Provisioner,
    ProvisioningInterrupted,
    ProvisioningLeaseLost,
    PsycopgTenantReplicator,
    PsycopgTenantAdmin,
    S3BucketAdmin,
    SqlAlchemyProvisioningRepository,
    TenantResourceRecord,
)

_WORKER_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CONNINFO_KEY_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=")
_ALLOWED_TENANT_ADMIN_CONNINFO_KEYS = frozenset(
    {
        "dbname",
        "host",
        "password",
        "port",
        "sslcert",
        "sslkey",
        "sslmode",
        "sslpassword",
        "sslrootcert",
        "user",
    }
)
_REQUIRED_TENANT_ADMIN_CONNINFO_KEYS = frozenset({"dbname", "host", "sslmode", "user"})
_INVALID_TENANT_ADMIN_CONNINFO = "tenant admin connection configuration is invalid"


class _Provisioner(Protocol):
    async def process_next(
        self,
        *,
        worker_id: str,
    ) -> TenantResourceRecord | None: ...


class ProvisionerWorker:
    def __init__(
        self,
        *,
        provisioner: _Provisioner,
        worker_id: str,
        idle_poll_seconds: float = 1.0,
        failure_backoff_seconds: float = 2.0,
        close_callbacks: tuple[Any, ...] = (),
    ) -> None:
        if not _WORKER_ID_PATTERN.fullmatch(worker_id):
            raise ValueError("worker identity is invalid")
        if idle_poll_seconds <= 0 or failure_backoff_seconds <= 0:
            raise ValueError("worker polling intervals must be positive")
        self._provisioner = provisioner
        self._worker_id = worker_id
        self._idle_poll_seconds = idle_poll_seconds
        self._failure_backoff_seconds = failure_backoff_seconds
        self._close_callbacks = close_callbacks

    async def run_once(self) -> bool:
        try:
            result = await self._provisioner.process_next(worker_id=self._worker_id)
        except (ProvisioningInterrupted, ProvisioningLeaseLost):
            return False
        return result is not None

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            try:
                worked = await self.run_once()
                delay = 0 if worked else self._idle_poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = self._failure_backoff_seconds
            if delay == 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def close(self) -> None:
        for callback in reversed(self._close_callbacks):
            result = callback()
            if hasattr(result, "__await__"):
                await result


def _read_owner_only_file(path: Path) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or (hasattr(os, "geteuid") and details.st_uid != os.geteuid())
            or stat.S_IMODE(details.st_mode) not in {0o400, 0o600}
        ):
            raise RuntimeError("provisioner secret file permissions are invalid")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > 1024 * 1024:
                raise RuntimeError("provisioner secret file is too large")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _required_environment(name: str) -> str:
    value = os.getenv(name, "")
    if not value or value != value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _keyword_conninfo_keys(conninfo: str) -> list[str]:
    keys: list[str] = []
    offset = 0
    while offset < len(conninfo):
        while offset < len(conninfo) and conninfo[offset].isspace():
            offset += 1
        if offset == len(conninfo):
            break
        match = _CONNINFO_KEY_PATTERN.match(conninfo, offset)
        if match is None:
            raise ValueError
        keys.append(match.group(1))
        offset = match.end()
        if offset < len(conninfo) and conninfo[offset] == "'":
            offset += 1
            while offset < len(conninfo):
                if conninfo[offset] == "\\":
                    offset += 2
                elif conninfo[offset] == "'":
                    offset += 1
                    break
                else:
                    offset += 1
            else:
                raise ValueError
            if offset < len(conninfo) and not conninfo[offset].isspace():
                raise ValueError
        else:
            while offset < len(conninfo) and not conninfo[offset].isspace():
                if conninfo[offset] == "\\":
                    offset += 2
                else:
                    offset += 1
        if offset > len(conninfo):
            raise ValueError
    return keys


def _uri_conninfo_keys(conninfo: str) -> list[str]:
    parsed = urlsplit(conninfo)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError
    keys: list[str] = []
    if parsed.username is not None:
        keys.append("user")
    if parsed.password is not None:
        keys.append("password")
    if parsed.hostname is not None:
        keys.append("host")
    if parsed.port is not None:
        keys.append("port")
    if parsed.path not in {"", "/"}:
        keys.append("dbname")
    keys.extend(
        key
        for key, _ in parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    )
    return keys


def _validate_tenant_admin_conninfo(raw_conninfo: bytes) -> str:
    try:
        conninfo = raw_conninfo.decode("utf-8").strip()
        if not conninfo:
            raise ValueError
        if conninfo.startswith(("postgres://", "postgresql://")):
            keys = _uri_conninfo_keys(conninfo)
        else:
            keys = _keyword_conninfo_keys(conninfo)
        if len(keys) != len(set(keys)) or not set(keys).issubset(
            _ALLOWED_TENANT_ADMIN_CONNINFO_KEYS
        ):
            raise ValueError
        parameters = conninfo_to_dict(conninfo)
        if any(
            not parameters.get(required, "").strip()
            for required in _REQUIRED_TENANT_ADMIN_CONNINFO_KEYS
        ):
            raise ValueError
        if parameters["sslmode"] != "verify-full" or "," in parameters["host"]:
            raise ValueError
    except Exception:
        raise RuntimeError(_INVALID_TENANT_ADMIN_CONNINFO) from None
    return conninfo


def _load_factory(reference: str) -> Any:
    if reference.count(":") != 1:
        raise RuntimeError("factory reference must use module:function")
    module_name, factory_name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module_name), factory_name, None)
    if factory is None or not callable(factory):
        raise RuntimeError("configured factory is unavailable")
    return factory


def _build_configured_secret_store() -> EncryptedFileSecretStore:
    keyring_path = Path(_required_environment("PERFPILOT_SECRET_KEYRING_CONFIG"))
    try:
        keyring_config = json.loads(_read_owner_only_file(keyring_path))
        active_key_id = keyring_config["active_key_id"]
        raw_keys = keyring_config["keys"]
        if (
            not isinstance(active_key_id, str)
            or not isinstance(raw_keys, dict)
            or not raw_keys
            or any(
                not isinstance(key_id, str) or not isinstance(key_path, str)
                for key_id, key_path in raw_keys.items()
            )
        ):
            raise ValueError
        key_files = {key_id: Path(key_path) for key_id, key_path in raw_keys.items()}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("secret keyring configuration is invalid") from exc
    return EncryptedFileSecretStore(
        Path(_required_environment("PERFPILOT_SECRET_STORE_ROOT")),
        key_files=key_files,
        active_key_id=active_key_id,
    )


def _build_tenant_replicator(
    *,
    cluster_host: str,
    cluster_port: int,
    sslmode: str,
) -> Any:
    factory_reference = os.getenv("PERFPILOT_TENANT_REPLICATOR_FACTORY", "")
    factory = _load_factory(factory_reference) if factory_reference else PsycopgTenantReplicator
    replicator = factory(
        cluster_host=cluster_host,
        cluster_port=cluster_port,
        sslmode=sslmode,
    )
    if not callable(getattr(replicator, "copy_and_validate", None)):
        raise RuntimeError("tenant replicator is invalid")
    return replicator


def build_production_worker() -> ProvisionerWorker:
    """Build production dependencies only from explicit config and mounted secrets."""

    settings = get_settings()
    if settings.app_env != "production":
        raise RuntimeError("provisioner requires a production environment")
    worker_id = _required_environment("PERFPILOT_PROVISIONER_WORKER_ID")
    sites_origin = _required_environment("PERFPILOT_SITES_ORIGIN")
    cluster_host = _required_environment("PERFPILOT_TENANT_CLUSTER_HOST")
    cluster_port = int(os.getenv("PERFPILOT_TENANT_CLUSTER_PORT", "5432"))
    cluster_sslmode = os.getenv("PERFPILOT_TENANT_CLUSTER_SSLMODE", "verify-full")
    if cluster_sslmode != "verify-full":
        raise RuntimeError("production tenant cluster SSL mode must be verify-full")
    admin_conninfo_path = Path(_required_environment("PERFPILOT_TENANT_ADMIN_CONNINFO_FILE"))
    admin_conninfo = _validate_tenant_admin_conninfo(_read_owner_only_file(admin_conninfo_path))

    secret_store = _build_configured_secret_store()

    control_engine = create_control_engine(settings.control_database_url.get_secret_value())
    session_factory = create_control_session_factory(control_engine)
    repository = SqlAlchemyProvisioningRepository(session_factory=session_factory)
    route_repository = SqlAlchemyTenantRouteRepository(session_factory=session_factory)
    cluster = TenantClusterEndpoint(
        host=cluster_host,
        port=cluster_port,
        sslmode=cluster_sslmode,
    )
    tenant_router = TenantRouter(
        control_resources=route_repository,
        secret_store=secret_store,
        cluster=cluster,
    )
    postgres = PsycopgTenantAdmin(admin_conninfo=admin_conninfo)
    migrator = AlembicTenantMigrator(
        migration_root=Path(__file__).resolve().parents[3] / "migrations" / "tenant",
        cluster_host=cluster_host,
        cluster_port=cluster_port,
        sslmode=cluster_sslmode,
    )
    s3_client = boto3.client(
        "s3",
        endpoint_url=str(settings.s3_endpoint_url),
    )
    bucket_admin = S3BucketAdmin(client=s3_client)
    replicator = _build_tenant_replicator(
        cluster_host=cluster_host,
        cluster_port=cluster_port,
        sslmode=cluster_sslmode,
    )
    provisioner = Provisioner(
        repository=repository,
        postgres=postgres,
        secret_store=secret_store,
        migrator=migrator,
        bucket_admin=bucket_admin,
        tenant_router=tenant_router,
        replicator=replicator,
        sites_origin=sites_origin,
    )
    return ProvisionerWorker(
        provisioner=provisioner,
        worker_id=worker_id,
        close_callbacks=(
            secret_store.close,
            control_engine.dispose,
            tenant_router.dispose,
        ),
    )


def _load_explicit_worker() -> ProvisionerWorker:
    factory_reference = os.getenv("PERFPILOT_PROVISIONER_FACTORY", "")
    worker = _load_factory(factory_reference)() if factory_reference else build_production_worker()
    if not isinstance(worker, ProvisionerWorker):
        raise RuntimeError("provisioner factory returned an invalid worker")
    return worker


def main() -> None:
    worker = _load_explicit_worker()

    async def run() -> None:
        try:
            await worker.run_forever()
        finally:
            await worker.close()

    asyncio.run(run())


def secret_maintenance_main(argv: Sequence[str] | None = None) -> None:
    if get_settings().app_env != "production":
        raise RuntimeError("secret maintenance requires a production environment")
    parser = argparse.ArgumentParser(prog="perfpilot-secret-maintenance")
    commands = parser.add_subparsers(dest="operation", required=True)
    rotate_parser = commands.add_parser("rotate")
    rotate_parser.add_argument("--key-id", required=True)
    rotate_parser.add_argument("--key-file", required=True, type=Path)
    retire_parser = commands.add_parser("retire")
    retire_parser.add_argument("--key-id", required=True)
    arguments = parser.parse_args(argv)
    secret_store = _build_configured_secret_store()

    async def run() -> None:
        try:
            if arguments.operation == "rotate":
                await secret_store.rotate(
                    new_key_id=arguments.key_id,
                    new_key_file=arguments.key_file,
                )
            else:
                await secret_store.retire_key(arguments.key_id)
        finally:
            secret_store.close()

    asyncio.run(run())
