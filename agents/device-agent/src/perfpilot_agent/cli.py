from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path

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
    return parser


def _write_json(document: object) -> None:
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _load_registered(store: CredentialStore) -> AgentCredentials:
    credentials = store.load()
    if credentials is None:
        raise CredentialStoreError
    return credentials


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


async def _run(config_path: Path | None) -> int:
    config = load_config(config_path)
    store = CredentialStore(_credential_backend())
    credentials = _load_registered(store)
    config.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    adb = await resolve_adb(
        configured=config.adb_path,
        workspace_root=config.workspace_root,
    )
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
            inventory=create_device_inventory(binary=adb),
            control=control,
            credentials=credentials,
            metadata=metadata,
            state=state,
            workspace_root=config.workspace_root,
            redactor=redactor,
        )
        executor = TaskExecutor(
            control=control,
            runner=_capture_runner(
                config=config,
                adb_binary=adb,
                control=control,
                state=state,
                redactor=redactor,
            ),
            state=state,
        )
        tasks = TaskLoop(control=control, executor=executor, state=state)
        await AgentService(
            heartbeat=heartbeat,
            tasks=tasks,
            credentials=control,
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
    except (
        AdbError,
        ControlClientError,
        CredentialStoreError,
        ImportError,
        RegistrationError,
        TaskExecutionError,
        OSError,
        ValueError,
    ):
        print("PerfPilot Agent 操作失败，请检查配置、凭据、ADB 和网络连接。", file=sys.stderr)
        return 1
    return 2


__all__ = ["main"]
