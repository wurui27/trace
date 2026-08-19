from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path
from uuid import UUID

from perfpilot_agent.adb import AdbError, resolve_adb
from perfpilot_agent.config import AgentConfig, load_config
from perfpilot_agent.control_client import ControlClient, ControlClientError
from perfpilot_agent.credentials import (
    AgentCredentials,
    CredentialBackend,
    CredentialStore,
    CredentialStoreError,
)
from perfpilot_agent.devices import HeartbeatPublisher, create_device_inventory
from perfpilot_agent.executor import TaskExecutionError, TaskExecutor
from perfpilot_agent.logging import RedactingFilter, SecretRedactor
from perfpilot_agent.platform.base import current_platform_metadata, current_platform_name
from perfpilot_agent.registration import RegistrationError, RegistrationService
from perfpilot_agent.service import AgentService, TaskLoop
from perfpilot_agent.source_registry import SourceRegistryError, SourceWorkspaceRegistry
from perfpilot_agent.source_runner import SourceTaskRunner
from perfpilot_agent.state import AgentRuntimeState


def _credential_backend() -> CredentialBackend:
    platform_name = current_platform_name()
    if platform_name == "macos":
        from perfpilot_agent.platform.macos import MacOSKeychainCredentialBackend

        return MacOSKeychainCredentialBackend()
    if platform_name == "windows":
        from perfpilot_agent.platform.windows import WindowsDpapiCredentialBackend

        return WindowsDpapiCredentialBackend()
    from perfpilot_agent.platform.linux import LinuxFileCredentialBackend

    return LinuxFileCredentialBackend()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perfpilot-agent")
    parser.add_argument("--config", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="register this Agent")
    register.add_argument("--replace", action="store_true")
    commands.add_parser("run", help="run the Agent service")
    status = commands.add_parser("status", help="show redacted local status")
    status.add_argument("--json", action="store_true", required=True)
    doctor = commands.add_parser("doctor", help="check local Agent dependencies")
    doctor.add_argument("--json", action="store_true", required=True)
    unregister = commands.add_parser("unregister", help="revoke this Agent")
    unregister.add_argument("--local-only", action="store_true")

    source = commands.add_parser("source", help="manage Agent-local source workspaces")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_add = source_commands.add_parser("add", help="register a source workspace")
    source_add.add_argument("--name", required=True)
    source_add.add_argument("--path", type=Path, required=True)
    source_list = source_commands.add_parser("list", help="list source workspaces")
    source_list.add_argument("--json", action="store_true", required=True)
    source_remove = source_commands.add_parser("remove", help="remove a source workspace")
    source_remove.add_argument("--workspace-id", type=UUID, required=True)
    source_doctor = source_commands.add_parser("doctor", help="inspect a source workspace")
    source_doctor.add_argument("--workspace-id", type=UUID, required=True)
    source_doctor.add_argument("--json", action="store_true", required=True)

    validation = source_commands.add_parser("validation", help="manage validation profiles")
    validation_commands = validation.add_subparsers(
        dest="validation_command",
        required=True,
    )
    validation_add = validation_commands.add_parser("add", help="add a validation profile")
    validation_add.add_argument("--workspace-id", type=UUID, required=True)
    validation_add.add_argument("--name", required=True)
    validation_add.add_argument("--working-directory", required=True)
    validation_add.add_argument("--timeout-seconds", type=int, required=True)
    validation_add.add_argument(
        "--allowed-exit-code",
        type=int,
        action="append",
        required=True,
    )
    validation_add.add_argument("command_argv", nargs=argparse.REMAINDER)
    validation_list = validation_commands.add_parser("list", help="list validation profiles")
    validation_list.add_argument("--workspace-id", type=UUID, required=True)
    validation_list.add_argument("--json", action="store_true", required=True)
    validation_remove = validation_commands.add_parser(
        "remove",
        help="remove a validation profile",
    )
    validation_remove.add_argument("--workspace-id", type=UUID, required=True)
    validation_remove.add_argument("--profile-id", type=UUID, required=True)
    return parser


def _write_json(document: object) -> None:
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


async def _register(config_path: Path | None, replace: bool) -> int:
    config = load_config(config_path)
    store = CredentialStore(_credential_backend())
    existing = store.load()
    confirmed = False
    if existing is not None:
        if not replace:
            print("PerfPilot Agent 已注册；如需替换请使用 --replace。", file=sys.stderr)
            return 2
        confirmation = input("输入 REPLACE 确认替换本机 Agent 凭据：")
        confirmed = confirmation == "REPLACE"
        if not confirmed:
            print("已取消。", file=sys.stderr)
            return 2
    code_text = getpass.getpass("Agent 注册码：")
    try:
        code = bytearray(code_text.encode("ascii"))
    except UnicodeEncodeError:
        code = bytearray()
    code_text = ""
    async with ControlClient(config) as client:
        service = RegistrationService(
            store=store,
            client=client,
            metadata=current_platform_metadata(),
        )
        credentials = await service.register(
            code,
            replace=replace,
            replacement_confirmed=confirmed,
        )
    print(f"Agent 注册完成：{credentials.agent_id}")
    return 0


def _status(config_path: Path | None) -> int:
    load_config(config_path)
    credentials = CredentialStore(_credential_backend()).load()
    _write_json(
        {
            "schema_version": "1.0",
            "registered": credentials is not None,
            "agent_id": None if credentials is None else str(credentials.agent_id),
            "state": "stopped",
        }
    )
    return 0


async def _doctor(config_path: Path | None) -> int:
    config = load_config(config_path)
    credentials = CredentialStore(_credential_backend()).load()
    adb = await resolve_adb(
        configured=config.adb_path,
        workspace_root=config.workspace_root,
    )
    inventory = await create_device_inventory(binary=adb).read_all()
    state_counts = {
        state: sum(item.observation.adb_state == state for item in inventory)
        for state in ("device", "booting", "unauthorized", "offline")
    }
    _write_json(
        {
            "schema_version": "1.0",
            "config": "ok",
            "registered": credentials is not None,
            "adb": "ok",
            "device_count": len(inventory),
            "device_states": state_counts,
        }
    )
    return 0


def _source_registry(config: AgentConfig) -> SourceWorkspaceRegistry:
    return SourceWorkspaceRegistry(config.workspace_root)


def _source_task_runner(*, config: AgentConfig, control: ControlClient) -> SourceTaskRunner:
    return SourceTaskRunner(
        control=control,
        registry=_source_registry(config),
        cache_root=config.workspace_root / "source-cache",
    )


def _source(config_path: Path | None, arguments: argparse.Namespace) -> int:
    config = load_config(config_path)
    registry = _source_registry(config)
    if arguments.source_command == "add":
        workspace = registry.add(name=arguments.name, path=arguments.path)
        _write_json(workspace.public_document())
        return 0
    if arguments.source_command == "list":
        _write_json(list(registry.public_workspaces()))
        return 0
    if arguments.source_command == "remove":
        registry.remove(arguments.workspace_id)
        _write_json({"removed": True, "workspace_id": str(arguments.workspace_id)})
        return 0
    if arguments.source_command == "doctor":
        _write_json(registry.doctor(arguments.workspace_id))
        return 0
    if arguments.source_command != "validation":
        return 2
    if arguments.validation_command == "add":
        command_argv = arguments.command_argv
        if not command_argv or command_argv[0] != "--" or len(command_argv) == 1:
            print("Validation command must follow an explicit -- separator.", file=sys.stderr)
            return 2
        profile = registry.add_validation(
            workspace_id=arguments.workspace_id,
            name=arguments.name,
            argv=tuple(command_argv[1:]),
            working_directory=arguments.working_directory,
            timeout_seconds=arguments.timeout_seconds,
            allowed_exit_codes=tuple(arguments.allowed_exit_code),
        )
        _write_json(profile.public_document())
        return 0
    if arguments.validation_command == "list":
        _write_json(
            [
                profile.public_document()
                for profile in registry.list_validation(arguments.workspace_id)
            ]
        )
        return 0
    if arguments.validation_command == "remove":
        registry.remove_validation(arguments.workspace_id, arguments.profile_id)
        _write_json({"profile_id": str(arguments.profile_id), "removed": True})
        return 0
    return 2


def _configure_logging(redactor: SecretRedactor) -> None:
    logger = logging.getLogger("perfpilot-agent")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(RedactingFilter(redactor))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _capture_runner(
    *,
    config: AgentConfig,
    adb_binary: Path,
    control: ControlClient,
    state: AgentRuntimeState,
    redactor: SecretRedactor,
):
    from perfpilot_agent.capture import CaptureTaskRunner

    return CaptureTaskRunner(
        config=config,
        adb_binary=adb_binary,
        control=control,
        state=state,
        redactor=redactor,
    )


class _LazyAdb:
    def __init__(self, *, config: AgentConfig) -> None:
        self._config = config
        self._binary: Path | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Path:
        async with self._lock:
            if self._binary is None:
                self._binary = await resolve_adb(
                    configured=self._config.adb_path,
                    workspace_root=self._config.workspace_root,
                )
            return self._binary


class _LazyDeviceInventory:
    def __init__(self, adb: _LazyAdb) -> None:
        self._adb = adb

    async def read_all(self):
        try:
            return await create_device_inventory(binary=await self._adb.get()).read_all()
        except (AdbError, OSError):
            logging.getLogger("perfpilot-agent.cli").warning("ADB inventory unavailable")
            return ()


class _LazyCaptureExecutor:
    def __init__(
        self,
        *,
        config: AgentConfig,
        adb: _LazyAdb,
        control: ControlClient,
        state: AgentRuntimeState,
        redactor: SecretRedactor,
    ) -> None:
        self._config = config
        self._adb = adb
        self._control = control
        self._state = state
        self._redactor = redactor
        self._executor: TaskExecutor | None = None

    async def run(self, task: object) -> None:
        if self._executor is None:
            binary = await self._adb.get()
            self._executor = TaskExecutor(
                control=self._control,
                runner=_capture_runner(
                    config=self._config,
                    adb_binary=binary,
                    control=self._control,
                    state=self._state,
                    redactor=self._redactor,
                ),
                state=self._state,
            )
        await self._executor.run(task)


async def _run(config_path: Path | None) -> int:
    config = load_config(config_path)
    store = CredentialStore(_credential_backend())
    credentials = store.load()
    if credentials is None:
        async with ControlClient(config) as registration_client:
            credentials = await RegistrationService(
                store=store,
                client=registration_client,
                metadata=current_platform_metadata(),
            ).auto_register()
    config.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    adb = _LazyAdb(config=config)
    metadata = current_platform_metadata()
    state = AgentRuntimeState()
    redactor = SecretRedactor()
    redactor.replace_live_values(
        serials=(),
        secrets={credentials.access_token, credentials.refresh_token},
    )
    _configure_logging(redactor)
    async with ControlClient(
        config,
        credentials=credentials,
        credential_store=store,
    ) as control:
        heartbeat = HeartbeatPublisher(
            inventory=_LazyDeviceInventory(adb),
            control=control,
            credentials=credentials,
            metadata=metadata,
            state=state,
            workspace_root=config.workspace_root,
            redactor=redactor,
            source_registry=_source_registry(config),
        )
        executor = _LazyCaptureExecutor(
            config=config,
            adb=adb,
            control=control,
            state=state,
            redactor=redactor,
        )
        tasks = TaskLoop(
            control=control,
            executor=executor,
            source_executor=_source_task_runner(config=config, control=control),
            state=state,
        )

        async def recover_credentials() -> AgentCredentials:
            replacement = await RegistrationService(
                store=store,
                client=control,
                metadata=metadata,
            ).auto_register(replace=True)
            control.bind_credentials(replacement, store=store)
            return replacement

        await AgentService(
            heartbeat=heartbeat,
            tasks=tasks,
            credentials=control,
            credential_recovery=recover_credentials,
        ).run()
    return 0


async def _unregister(config_path: Path | None, *, local_only: bool) -> int:
    config = load_config(config_path)
    store = CredentialStore(_credential_backend())
    credentials = store.load()
    if credentials is None:
        print("PerfPilot Agent 尚未注册。")
        return 0
    if local_only:
        confirmation = input("输入 UNREGISTER 确认仅删除本机 Agent 凭据：")
        if confirmation != "UNREGISTER":
            print("已取消。", file=sys.stderr)
            return 2
    else:
        async with ControlClient(
            config,
            credentials=credentials,
            credential_store=store,
        ) as client:
            await client.unregister()
    store.delete()
    print("PerfPilot Agent 已注销。")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "register":
            return asyncio.run(_register(arguments.config, arguments.replace))
        if arguments.command == "run":
            return asyncio.run(_run(arguments.config))
        if arguments.command == "status":
            return _status(arguments.config)
        if arguments.command == "doctor":
            return asyncio.run(_doctor(arguments.config))
        if arguments.command == "unregister":
            return asyncio.run(_unregister(arguments.config, local_only=arguments.local_only))
        if arguments.command == "source":
            return _source(arguments.config, arguments)
    except (
        AdbError,
        ControlClientError,
        CredentialStoreError,
        ImportError,
        RegistrationError,
        SourceRegistryError,
        TaskExecutionError,
        OSError,
        ValueError,
    ):
        print("PerfPilot Agent 操作失败，请检查配置、凭据、ADB 和网络连接。", file=sys.stderr)
        return 1
    return 2


__all__ = ["main"]
