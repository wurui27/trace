from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from perfpilot_agent.config import load_config
from perfpilot_agent.control_client import ControlClient, ControlClientError
from perfpilot_agent.credentials import CredentialBackend, CredentialStore, CredentialStoreError
from perfpilot_agent.platform.base import current_platform_metadata, current_platform_name
from perfpilot_agent.registration import RegistrationError, RegistrationService


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
    return parser


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


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "register":
            return asyncio.run(_register(arguments.config, arguments.replace))
    except (ControlClientError, CredentialStoreError, RegistrationError, OSError, ValueError):
        print("Agent 注册失败，请检查配置、注册码和网络连接。", file=sys.stderr)
        return 1
    return 2


__all__ = ["main"]
