from __future__ import annotations

import json
import runpy
import stat
from pathlib import Path

from perfpilot_api.local_control_store import LocalControlStore


SCRIPT = Path(__file__).parents[4] / "scripts" / "bootstrap-local-users.py"


def _run_bootstrap(monkeypatch, state_root: Path, admin_file: Path) -> None:
    monkeypatch.setenv("PERFPILOT_LOCAL_STATE_DIR", str(state_root))
    monkeypatch.setenv("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE", str(admin_file))
    runpy.run_path(str(SCRIPT), run_name="__main__")


def test_bootstrap_creates_private_idempotent_users_without_secret_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state_root = tmp_path / "state"
    admin_file = tmp_path / "admin-password"
    admin_password = "owner supplied admin password"
    admin_file.write_text(admin_password + "\n", encoding="utf-8")
    admin_file.chmod(0o600)

    _run_bootstrap(monkeypatch, state_root, admin_file)
    credentials = state_root / "bootstrap-users.txt"
    first_credentials = credentials.read_bytes()
    lines = credentials.read_text(encoding="utf-8").splitlines()
    passwords = dict(line.split("=", 1) for line in lines)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert set(passwords) == {"ray_wu", "user01", "user02", "user03", "user04", "user05"}
    assert all(len(password) >= 18 for password in passwords.values())
    assert passwords["ray_wu"] == admin_password
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    store = LocalControlStore(state_root)
    admin = store.authenticate("ray_wu", admin_password)
    assert admin is not None and admin.is_platform_admin and admin.must_change_password
    for username in ("user01", "user02", "user03", "user04", "user05"):
        user = store.authenticate(username, passwords[username])
        assert user is not None and not user.is_platform_admin and user.must_change_password

    store.change_password(
        store.find_user("user01").user_id, passwords["user01"], "user-chosen replacement"
    )
    changed_document = (state_root / "control.json").read_bytes()
    _run_bootstrap(monkeypatch, state_root, admin_file)
    assert credentials.read_bytes() == first_credentials
    assert (state_root / "control.json").read_bytes() == changed_document
    assert LocalControlStore(state_root).authenticate("user01", "user-chosen replacement")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_bootstrap_generates_private_admin_source_when_no_file_is_supplied(
    tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("PERFPILOT_LOCAL_STATE_DIR", str(state_root))
    monkeypatch.delenv("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE", raising=False)

    runpy.run_path(str(SCRIPT), run_name="__main__")

    admin_file = state_root / "bootstrap-admin-password.txt"
    password = admin_file.read_text(encoding="utf-8").rstrip("\n")
    assert len(password) >= 18
    assert stat.S_IMODE(admin_file.stat().st_mode) == 0o600
    assert LocalControlStore(state_root).authenticate("ray_wu", password) is not None


def test_bootstrap_preserves_existing_password_role_and_team(
    tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    old_password = "existing ordinary password"
    original = LocalControlStore(state_root).ensure_user("user01", old_password, False).principal
    admin_file = tmp_path / "admin-password"
    admin_file.write_text("new administrator password\n", encoding="utf-8")
    admin_file.chmod(0o600)

    _run_bootstrap(monkeypatch, state_root, admin_file)

    reopened = LocalControlStore(state_root)
    existing = reopened.authenticate("user01", old_password)
    assert existing == original
    assert existing is not None and existing.team_id == original.team_id
    assert reopened.authenticate("user01", "new administrator password") is None
    credentials = (state_root / "bootstrap-users.txt").read_text(encoding="utf-8")
    assert "user01=" not in credentials


def test_find_user_is_read_only_and_does_not_require_a_password(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path)
    created = store.ensure_user("ray_wu", "stable existing password", True).principal
    before = (tmp_path / "control.json").read_bytes()

    assert store.find_user("ＲＡＹ_ＷＵ") == created
    assert store.find_user("missing") is None
    assert (tmp_path / "control.json").read_bytes() == before


def test_bootstrap_resumes_journal_after_user_creation_crash(
    tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    admin_file = tmp_path / "admin-password"
    admin_file.write_text("recoverable administrator password\n", encoding="utf-8")
    admin_file.chmod(0o600)
    original_ensure_user = LocalControlStore.ensure_user

    def crash_after_user01(self, username: str, password: str, admin: bool):
        result = original_ensure_user(self, username, password, admin)
        if username == "user01":
            raise RuntimeError("injected crash after user01")
        return result

    monkeypatch.setattr(LocalControlStore, "ensure_user", crash_after_user01)
    try:
        _run_bootstrap(monkeypatch, state_root, admin_file)
    except RuntimeError as error:
        assert str(error) == "injected crash after user01"
    else:
        raise AssertionError("injected bootstrap crash did not occur")

    pending = state_root / "bootstrap-users.pending.json"
    assert pending.exists()
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert not (state_root / "bootstrap-users.txt").exists()
    journal = json.loads(pending.read_text(encoding="utf-8"))
    user01_password = next(
        item["password"] for item in journal["users"] if item["username"] == "user01"
    )
    assert LocalControlStore(state_root).authenticate("user01", user01_password) is not None

    monkeypatch.setattr(LocalControlStore, "ensure_user", original_ensure_user)
    _run_bootstrap(monkeypatch, state_root, admin_file)

    credentials = (state_root / "bootstrap-users.txt").read_text(encoding="utf-8")
    assert f"user01={user01_password}\n" in credentials
    assert not pending.exists()
    assert LocalControlStore(state_root).authenticate("user01", user01_password) is not None
