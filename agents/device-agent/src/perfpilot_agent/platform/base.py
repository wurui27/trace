from __future__ import annotations

import platform as host_platform
import socket
import sys
from dataclasses import dataclass
from typing import Literal

AgentPlatform = Literal["macos", "windows", "linux"]


@dataclass(frozen=True, slots=True)
class PlatformMetadata:
    platform: AgentPlatform
    hostname: str
    os_version: str


def current_platform_name(platform_name: str | None = None) -> AgentPlatform:
    current = platform_name or sys.platform
    if current == "darwin":
        return "macos"
    if current == "win32":
        return "windows"
    if current.startswith("linux"):
        return "linux"
    raise RuntimeError("PerfPilot Agent does not support this platform")


def current_platform_metadata() -> PlatformMetadata:
    hostname = socket.gethostname().strip()
    os_version = host_platform.platform(aliased=True, terse=False).strip()
    if not hostname or len(hostname) > 200 or any(ord(character) < 32 for character in hostname):
        raise RuntimeError("PerfPilot Agent hostname is invalid")
    if not os_version or len(os_version) > 128:
        os_version = host_platform.release().strip()
    if not os_version or len(os_version) > 128:
        raise RuntimeError("PerfPilot Agent OS version is invalid")
    return PlatformMetadata(
        platform=current_platform_name(),
        hostname=hostname,
        os_version=os_version,
    )


__all__ = [
    "AgentPlatform",
    "PlatformMetadata",
    "current_platform_metadata",
    "current_platform_name",
]
