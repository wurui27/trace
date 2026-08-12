#!/usr/bin/env python3
"""Idempotently create the private Ubuntu test deployment's local users."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from perfpilot_api.local_control_store import LocalControlStore, LocalControlStoreError


USERS = (("ray_wu", True),) + tuple((f"user{index:02d}", False) for index in range(1, 6))


def _read_private_secret(path: Path) -> str:
    if path.is_symlink():
        raise LocalControlStoreError("unsafe bootstrap credential path")
    status = path.stat()
    if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o600:
        raise LocalControlStoreError("unsafe bootstrap credential path")
    secret = path.read_text(encoding="utf-8").rstrip("\n")
    if len(secret) < 18:
        raise LocalControlStoreError("invalid bootstrap credential")
    return secret


def _exclusive_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> None:
    state_root_text = os.environ.pop("PERFPILOT_LOCAL_STATE_DIR", "")
    password_file_text = os.environ.pop("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE", "")
    if not state_root_text:
        raise LocalControlStoreError("missing local state directory")
    state_root = Path(state_root_text)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    credentials_file = state_root / "bootstrap-users.txt"
    admin_file = (
        Path(password_file_text)
        if password_file_text
        else state_root / "bootstrap-admin-password.txt"
    )

    store = LocalControlStore(state_root)
    missing = [(username, admin) for username, admin in USERS if store.find_user(username) is None]
    if not missing:
        return
    if credentials_file.exists() or credentials_file.is_symlink():
        raise LocalControlStoreError("bootstrap credentials already exist")

    admin_password: str | None = None
    generated_admin_password = False
    if any(admin for _, admin in missing):
        if admin_file.exists() or admin_file.is_symlink():
            admin_password = _read_private_secret(admin_file)
        else:
            admin_password = secrets.token_urlsafe(24)
            generated_admin_password = True

    if generated_admin_password:
        assert admin_password is not None
        _exclusive_write(admin_file, admin_password + "\n")

    created_credentials: list[tuple[str, str]] = []
    for username, admin in missing:
        password = admin_password if admin else secrets.token_urlsafe(24)
        assert password is not None and len(password) >= 18
        result = store.ensure_user(username, password, admin)
        if result.created:
            created_credentials.append((username, password))

    body = "".join(f"{username}={password}\n" for username, password in created_credentials)
    _exclusive_write(credentials_file, body)


if __name__ == "__main__":
    main()
