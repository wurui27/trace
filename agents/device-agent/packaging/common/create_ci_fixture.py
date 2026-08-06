from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

_PATHS = {
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


def create(*, platform_name: str, output: Path) -> None:
    if platform_name not in _PATHS:
        raise ValueError("fixture platform is invalid")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PerfPilot CI Root")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private_key, algorithm=None)
    )
    ca_path = output / "perfpilot-ca.crt"
    ca_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    os.chmod(ca_path, 0o644)
    config = {
        "schema_version": "1.0",
        "server_url": "https://127.0.0.1",
        **_PATHS[platform_name],
    }
    config_path = output / "config.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=tuple(_PATHS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    create(platform_name=arguments.platform, output=arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
