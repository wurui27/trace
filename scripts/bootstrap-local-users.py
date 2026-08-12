#!/usr/bin/env python3
"""Idempotently create the private Ubuntu test deployment's local users."""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

from perfpilot_api.local_control_store import LocalControlStore, LocalControlStoreError


USERS = (("ray_wu", True),) + tuple((f"user{index:02d}", False) for index in range(1, 6))
_JOURNAL_VERSION = 1


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
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _remove_durable(path: Path) -> None:
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_journal(path: Path) -> list[tuple[str, str, bool]]:
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise LocalControlStoreError("unsafe bootstrap credential path")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        raw_users = document["users"]
        if document != {"version": _JOURNAL_VERSION, "users": raw_users}:
            raise ValueError
        users = [
            (item["username"], item["password"], item["admin"])
            for item in raw_users
        ]
        if any(
            item != {"username": username, "password": password, "admin": admin}
            or (username, admin) not in USERS
            or not isinstance(password, str)
            or len(password) < 18
            for item, (username, password, admin) in zip(raw_users, users, strict=True)
        ):
            raise ValueError
        if len({username for username, _, _ in users}) != len(users):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise LocalControlStoreError("invalid bootstrap credential journal") from None
    return users


def _journal_content(users: list[tuple[str, str, bool]]) -> str:
    return json.dumps(
        {
            "version": _JOURNAL_VERSION,
            "users": [
                {"username": username, "password": password, "admin": admin}
                for username, password, admin in users
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ) + "\n"


def main() -> None:
    state_root_text = os.environ.pop("PERFPILOT_LOCAL_STATE_DIR", "")
    password_file_text = os.environ.pop("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE", "")
    if not state_root_text:
        raise LocalControlStoreError("missing local state directory")
    state_root = Path(state_root_text)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    credentials_file = state_root / "bootstrap-users.txt"
    pending_file = state_root / "bootstrap-users.pending.json"
    admin_file = (
        Path(password_file_text)
        if password_file_text
        else state_root / "bootstrap-admin-password.txt"
    )

    if credentials_file.is_symlink():
        raise LocalControlStoreError("unsafe bootstrap credential path")
    if credentials_file.exists():
        if pending_file.exists() and not pending_file.is_symlink():
            _remove_durable(pending_file)
        return

    store = LocalControlStore(state_root)
    if pending_file.exists() or pending_file.is_symlink():
        journal_users = _read_journal(pending_file)
    else:
        missing = [
            (username, admin) for username, admin in USERS if store.find_user(username) is None
        ]
        if not missing:
            return

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

        journal_users = []
        for username, admin in missing:
            password = admin_password if admin else secrets.token_urlsafe(24)
            assert password is not None and len(password) >= 18
            journal_users.append((username, password, admin))
        _exclusive_write(pending_file, _journal_content(journal_users))

    for username, password, admin in journal_users:
        result = store.ensure_user(username, password, admin)
        if not result.created and store.authenticate(username, password) is None:
            raise LocalControlStoreError("bootstrap journal credential conflict")

    body = "".join(f"{username}={password}\n" for username, password, _ in journal_users)
    _exclusive_write(credentials_file, body)
    _remove_durable(pending_file)


if __name__ == "__main__":
    main()
