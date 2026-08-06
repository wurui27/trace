from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.x509.oid import ExtensionOID

_CONFIG_KEYS = {
    "schema_version",
    "server_url",
    "ca_bundle",
    "adb_path",
    "workspace_root",
}
_SECRET_KEY = re.compile(r"(?:credential|password|private|registration|secret|token)", re.I)
_EXPECTED = {
    "macos": {
        "ca_bundle": "/Library/Application Support/PerfPilot Agent/perfpilot-ca.crt",
        "adb_path": "/Library/PerfPilot Agent/platform-tools/adb",
        "workspace_root": "/Library/Application Support/PerfPilot Agent/workspace",
    },
    "windows": {
        "ca_bundle": r"C:\ProgramData\PerfPilot\Agent\perfpilot-ca.crt",
        "adb_path": r"C:\Program Files\PerfPilot Agent\platform-tools\adb.exe",
        "workspace_root": r"C:\ProgramData\PerfPilot\Agent\workspace",
    },
    "linux": {
        "ca_bundle": "/etc/perfpilot-agent/perfpilot-ca.crt",
        "adb_path": "/opt/perfpilot-agent/platform-tools/adb",
        "workspace_root": "/var/lib/perfpilot-agent/workspace",
    },
}


def _regular_file(path: Path) -> bytes:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("bootstrap input must be an absolute regular file")
    payload = path.read_bytes()
    if not payload or len(payload) > 64 * 1024:
        raise ValueError("bootstrap input size is invalid")
    return payload


def _validate_origin(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("server URL is invalid")
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
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("server URL is invalid")


def validate(*, platform_name: str, config_path: Path, ca_path: Path) -> None:
    if platform_name not in _EXPECTED:
        raise ValueError("bootstrap platform is invalid")
    try:
        config = json.loads(_regular_file(config_path))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("bootstrap configuration is invalid") from None
    if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
        raise ValueError("bootstrap configuration is invalid")
    if any(_SECRET_KEY.search(str(key)) for key in config):
        raise ValueError("bootstrap configuration contains a secret field")
    if config.get("schema_version") != "1.0":
        raise ValueError("bootstrap configuration is invalid")
    _validate_origin(config.get("server_url"))
    for key, expected in _EXPECTED[platform_name].items():
        if config.get(key) != expected:
            raise ValueError("bootstrap path does not match the native package")
    try:
        certificate = x509.load_pem_x509_certificate(_regular_file(ca_path))
        constraints = certificate.extensions.get_extension_for_oid(
            ExtensionOID.BASIC_CONSTRAINTS
        ).value
    except (ValueError, x509.ExtensionNotFound):
        raise ValueError("bootstrap CA is invalid") from None
    if not constraints.ca:
        raise ValueError("bootstrap CA is invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=tuple(_EXPECTED), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ca", type=Path, required=True)
    arguments = parser.parse_args()
    validate(
        platform_name=arguments.platform,
        config_path=arguments.config.resolve(),
        ca_path=arguments.ca.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
