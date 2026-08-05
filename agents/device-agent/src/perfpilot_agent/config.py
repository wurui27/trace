from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MAXIMUM_CONFIG_BYTES = 64 * 1024


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    server_url: str = Field(min_length=9, max_length=2_048)
    ca_bundle: Path
    adb_path: Path | None = None
    workspace_root: Path

    @field_validator("server_url")
    @classmethod
    def validate_server_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            raise ValueError("server URL is invalid") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("server URL must be a closed HTTPS origin")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        if not self.ca_bundle.is_absolute():
            raise ValueError("CA bundle path must be absolute")
        if (
            not self.ca_bundle.is_file()
            or self.ca_bundle.is_symlink()
            or not os.access(self.ca_bundle, os.R_OK)
        ):
            raise ValueError("CA bundle must be a readable regular file")
        if not self.workspace_root.is_absolute():
            raise ValueError("workspace root must be absolute")
        if self.adb_path is not None and not self.adb_path.is_absolute():
            raise ValueError("ADB path must be absolute")
        return self


def default_config_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    current = platform_name or sys.platform
    environment = os.environ if environ is None else environ
    if current == "darwin":
        return Path("/Library/Application Support/PerfPilot Agent/config.json")
    if current == "win32":
        program_data = environment.get("ProgramData")
        if not program_data:
            raise RuntimeError("PerfPilot Agent configuration is unavailable")
        return Path(program_data) / "PerfPilot" / "Agent" / "config.json"
    if current.startswith("linux"):
        return Path("/etc/perfpilot-agent/config.json")
    raise RuntimeError("PerfPilot Agent does not support this platform")


def load_config(path: Path | None = None) -> AgentConfig:
    target = path or default_config_path()
    with target.open("rb") as source:
        payload = source.read(_MAXIMUM_CONFIG_BYTES + 1)
    if len(payload) > _MAXIMUM_CONFIG_BYTES:
        raise ValueError("PerfPilot Agent configuration is too large")
    return AgentConfig.model_validate_json(payload)


__all__ = ["AgentConfig", "default_config_path", "load_config"]
