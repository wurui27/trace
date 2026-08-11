from __future__ import annotations

import json
import multiprocessing
import os
import queue
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.local_control_store import (
    LOCAL_SESSION_TTL,
    LocalControlStore,
    LocalControlStoreError,
    LocalControlStoreNotFoundError,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _concurrent_bootstrap(state_root: str, username: str) -> None:
    LocalControlStore(Path(state_root)).ensure_user(username, "safe local password", False)


def _construct_store_with_fifo(state_root: str, results: multiprocessing.Queue[str]) -> None:
    try:
        LocalControlStore(Path(state_root))
    except LocalControlStoreError as error:
        results.put(str(error))
    else:
        results.put("unexpected success")


def _load_fifo_after_construction(state_root: str, results: multiprocessing.Queue[str]) -> None:
    store = LocalControlStore(Path(state_root))
    control = Path(state_root) / "control.json"
    os.mkfifo(control, 0o600)
    try:
        store.authenticate("ordinary", "valid password")
    except LocalControlStoreError as error:
        results.put(str(error))
    else:
        results.put("unexpected success")


def _assert_child_finishes_redacted(
    process: multiprocessing.Process,
    results: multiprocessing.Queue[str],
) -> None:
    process.start()
    process.join(timeout=3)
    if process.is_alive():
        process.terminate()
        process.join(timeout=3)
        pytest.fail("FIFO operation blocked")
    assert process.exitcode == 0
    try:
        assert results.get(timeout=1) in {
            "unsafe local control path",
            "invalid local control state",
        }
    except queue.Empty:
        pytest.fail("FIFO child did not report a redacted error")


def test_ensure_user_is_idempotent_and_reopen_preserves_ids_and_changed_password(
    tmp_path: Path,
) -> None:
    store = LocalControlStore(tmp_path)
    created = store.ensure_user("  ＲＡＹ_ＷＵ  ", "initial secret", True)
    changed = store.change_password(
        created.principal.user_id,
        "initial secret",
        "replacement password secret",
    )
    existing = LocalControlStore(tmp_path).ensure_user("ray_wu", "bootstrap secret", False)

    assert created.created is True
    assert existing.created is False
    assert existing.principal.user_id == created.principal.user_id
    assert existing.principal.team_id == created.principal.team_id
    assert existing.principal.is_platform_admin is True
    assert changed.must_change_password is False
    assert store.authenticate("ray_wu", "bootstrap secret") is None
    assert store.authenticate("ray_wu", "replacement password secret") == changed


def test_control_file_contains_only_password_hash_and_has_private_permissions(tmp_path: Path) -> None:
    secret = "plaintext password must never persist"
    store = LocalControlStore(tmp_path)
    store.ensure_user("ordinary", secret, False)

    payload = (tmp_path / "control.json").read_text(encoding="utf-8")
    document = json.loads(payload)
    assert secret not in payload
    assert set(document) == {"schema_version", "users", "teams", "sessions"}
    assert "password" not in document["users"][0]
    assert document["users"][0]["password_hash"].startswith("$argon2")
    assert document["users"][0]["team_id"] == document["teams"][0]["team_id"]
    assert document["teams"][0]["name"] == "ordinary local team"
    assert (tmp_path.stat().st_mode & 0o777) == 0o700
    assert ((tmp_path / "control.json").stat().st_mode & 0o777) == 0o600


def test_reopens_user_and_separate_team_records_with_stable_factory_ids(
    tmp_path: Path,
) -> None:
    expected_user_id = UUID("80000000-0000-4000-8000-000000000001")
    expected_team_id = UUID("81000000-0000-4000-8000-000000000001")
    identifiers = iter((expected_user_id, expected_team_id))
    created = LocalControlStore(
        tmp_path,
        uuid_factory=lambda: next(identifiers),
    ).ensure_user("ordinary", "valid password", False)
    reopened = LocalControlStore(tmp_path).ensure_user("ordinary", "other valid password", True)
    document = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))

    assert created.principal.user_id == expected_user_id
    assert created.principal.team_id == expected_team_id
    assert reopened.principal == created.principal
    assert document["users"][0]["team_id"] == str(expected_team_id)
    assert document["teams"] == [
        {"team_id": str(expected_team_id), "name": "ordinary local team"}
    ]


def test_sessions_store_digests_expire_at_boundary_and_password_change_revokes_them(
    tmp_path: Path,
) -> None:
    instant = [NOW]
    tokens = iter(("session plaintext", "csrf plaintext"))
    store = LocalControlStore(
        tmp_path,
        clock=lambda: instant[0],
        token_factory=lambda: next(tokens),
    )
    user = store.ensure_user("ordinary", "current secret", False).principal
    session_token, csrf_token = store.issue_session(user.user_id)

    persisted = (tmp_path / "control.json").read_text(encoding="utf-8")
    assert session_token not in persisted
    assert csrf_token not in persisted
    assert "token_digest" in persisted
    assert store.resolve_session("unknown") is None
    instant[0] = NOW + LOCAL_SESSION_TTL
    assert store.resolve_session(session_token) is None
    instant[0] = NOW
    tokens = iter(("another session", "another csrf"))
    session_token, _ = store.issue_session(user.user_id)
    store.change_password(user.user_id, "current secret", "new replacement secret")
    assert store.resolve_session(session_token) is None


def test_preauth_sessions_verify_csrf_and_can_be_revoked(tmp_path: Path) -> None:
    tokens = iter(("preauth plaintext", "preauth csrf"))
    store = LocalControlStore(tmp_path, token_factory=lambda: next(tokens))

    session_token, csrf_token = store.issue_preauth_session()

    persisted = (tmp_path / "control.json").read_text(encoding="utf-8")
    assert session_token not in persisted
    assert csrf_token not in persisted
    assert store.verify_csrf(session_token, csrf_token, purpose="preauth") is True
    assert store.verify_csrf(session_token, csrf_token, purpose="authenticated") is False
    store.revoke_session(session_token)
    assert store.verify_csrf(session_token, csrf_token, purpose="preauth") is False


def test_reopens_a_v1_authenticated_session_as_a_v2_session(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path)
    user = store.ensure_user("ordinary", "valid local password", False).principal
    token, _ = store.issue_session(user.user_id)
    document = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    document["schema_version"] = 1
    for session in document["sessions"]:
        del session["purpose"]
    (tmp_path / "control.json").write_text(json.dumps(document), encoding="utf-8")

    reopened = LocalControlStore(tmp_path)

    assert reopened.resolve_session(token) == user
    assert json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))["schema_version"] == 2


def test_rejects_malformed_unknown_key_and_symlinked_control_paths_without_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / "control.json").write_text('{"schema_version":1,"users":[],"sessions":[],"secret":"no"}')
    with pytest.raises(LocalControlStoreError, match="invalid local control state") as malformed:
        LocalControlStore(tmp_path).authenticate("user", "secret marker")
    assert "secret marker" not in str(malformed.value)

    root_link = tmp_path / "linked-root"
    root_link.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(LocalControlStoreError, match="unsafe local control path"):
        LocalControlStore(root_link)

    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    linked_state = tmp_path / "linked-state"
    linked_state.mkdir()
    (linked_state / "control.json").symlink_to(outside)
    with pytest.raises(LocalControlStoreError, match="unsafe local control path"):
        LocalControlStore(linked_state)


def test_rejects_syntactically_invalid_and_duplicate_key_control_json(tmp_path: Path) -> None:
    (tmp_path / "control.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LocalControlStoreError, match="invalid local control state"):
        LocalControlStore(tmp_path).authenticate("person", "valid password")

    (tmp_path / "control.json").write_text(
        '{"schema_version":1,"schema_version":1,"users":[],"sessions":[]}',
        encoding="utf-8",
    )
    with pytest.raises(LocalControlStoreError, match="invalid local control state"):
        LocalControlStore(tmp_path).authenticate("person", "valid password")


def test_rejects_symlinked_lock_without_touching_its_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("do not modify", encoding="utf-8")
    (tmp_path / ".control.lock").symlink_to(victim)

    with pytest.raises(LocalControlStoreError, match="unsafe local control path"):
        LocalControlStore(tmp_path)

    assert victim.read_text(encoding="utf-8") == "do not modify"


def test_constructor_root_swap_cannot_change_outside_permissions_or_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    outside = tmp_path / "outside"
    state_root.mkdir()
    outside.mkdir()
    outside.chmod(0o755)
    victim = outside / "victim"
    victim.write_text("do not modify", encoding="utf-8")
    original_chmod = os.chmod

    def swap_before_path_chmod(path: Path | str, mode: int, *args: object, **kwargs: object) -> None:
        if Path(path) == state_root:
            state_root.rename(tmp_path / "moved-state")
            state_root.symlink_to(outside, target_is_directory=True)
        original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr("perfpilot_api.local_control_store.os.chmod", swap_before_path_chmod)
    LocalControlStore(state_root)

    assert (outside.stat().st_mode & 0o777) == 0o755
    assert victim.read_text(encoding="utf-8") == "do not modify"


def test_constructor_detects_root_swap_after_pinned_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    outside = tmp_path / "outside"
    state_root.mkdir()
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("do not modify", encoding="utf-8")
    original_open = os.open

    def open_then_swap(path: Path | str, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == state_root and flags & os.O_DIRECTORY:
            state_root.rename(tmp_path / "moved-state")
            state_root.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr("perfpilot_api.local_control_store.os.open", open_then_swap)
    with pytest.raises(LocalControlStoreError, match="unsafe local control path"):
        LocalControlStore(state_root)

    assert victim.read_text(encoding="utf-8") == "do not modify"


def test_fifo_control_file_rejects_without_blocking_during_construction(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "control.json", 0o600)
    context = multiprocessing.get_context("spawn")
    results: multiprocessing.Queue[str] = context.Queue()
    process = context.Process(target=_construct_store_with_fifo, args=(str(tmp_path), results))

    _assert_child_finishes_redacted(process, results)


def test_fifo_control_file_rejects_without_blocking_after_construction(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    results: multiprocessing.Queue[str] = context.Queue()
    process = context.Process(target=_load_fifo_after_construction, args=(str(tmp_path), results))

    _assert_child_finishes_redacted(process, results)


def test_rejects_root_symlink_substitution_after_store_construction(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    outside = tmp_path / "outside"
    state_root.mkdir()
    outside.mkdir()
    victim = outside / "control.json"
    victim.write_text("do not modify", encoding="utf-8")
    store = LocalControlStore(state_root)
    moved_state = tmp_path / "moved-state"
    state_root.rename(moved_state)
    state_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalControlStoreError, match="unsafe local control path"):
        store.ensure_user("ordinary", "valid password", False)

    assert victim.read_text(encoding="utf-8") == "do not modify"
    assert not (outside / ".control.lock").exists()
    assert not (outside / "control.json").is_symlink()


def test_rejects_root_swap_during_a_transaction_without_writing_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    outside = tmp_path / "outside"
    state_root.mkdir()
    outside.mkdir()
    victim = outside / "control.json"
    victim.write_text("do not modify", encoding="utf-8")
    store = LocalControlStore(state_root)
    original_read = store._read_document

    def swap_root_then_read() -> dict[str, object]:
        state_root.rename(tmp_path / "moved-state")
        state_root.symlink_to(outside, target_is_directory=True)
        return original_read()

    monkeypatch.setattr(store, "_read_document", swap_root_then_read)
    with pytest.raises(LocalControlStoreError, match="unsafe local control path"):
        store.ensure_user("ordinary", "valid password", False)

    assert victim.read_text(encoding="utf-8") == "do not modify"


def test_failed_atomic_replace_keeps_previous_control_file_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalControlStore(tmp_path)
    original = store.ensure_user("first", "safe password", False).principal

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr("perfpilot_api.local_control_store.os.replace", fail_replace)
    with pytest.raises(LocalControlStoreError, match="local control persistence failed"):
        store.ensure_user("second", "another safe password", False)

    assert LocalControlStore(tmp_path).authenticate("first", "safe password") == original
    assert LocalControlStore(tmp_path).authenticate("second", "another safe password") is None


def test_two_processes_serialise_updates_without_losing_users(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    first = context.Process(target=_concurrent_bootstrap, args=(str(tmp_path), "first"))
    second = context.Process(target=_concurrent_bootstrap, args=(str(tmp_path), "second"))
    first.start()
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)

    assert first.exitcode == 0
    assert second.exitcode == 0
    reopened = LocalControlStore(tmp_path)
    assert reopened.authenticate("first", "safe local password") is not None
    assert reopened.authenticate("second", "safe local password") is not None


def test_first_lock_creation_handles_a_concurrent_creator_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalControlStore(tmp_path)
    original_open = os.open
    injected_collision = False

    def create_competitor_then_report_exists(
        path: Path | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal injected_collision
        if path == ".control.lock" and flags & os.O_CREAT:
            assert flags & os.O_EXCL
            if not injected_collision:
                injected_collision = True
                descriptor = original_open(path, flags, *args, **kwargs)
                os.close(descriptor)
                raise FileExistsError
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        "perfpilot_api.local_control_store.os.open",
        create_competitor_then_report_exists,
    )
    created = store.ensure_user("ordinary", "valid password", False)

    assert injected_collision is True
    assert created.created is True


def test_normalization_collision_password_validation_and_team_requirement(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path)
    created = store.ensure_user("  Stra\u00dfe ", "valid password", False)
    duplicate = store.ensure_user("STRASSE", "other valid password", True)
    token, _ = store.issue_session(created.principal.user_id)

    assert duplicate.created is False
    assert store.authenticate("strasse", "valid password") == created.principal
    with pytest.raises(LocalControlStoreError, match="invalid local credentials"):
        store.ensure_user("   ", "valid password", False)
    with pytest.raises(LocalControlStoreError, match="invalid local credentials"):
        store.ensure_user("person", "", False)
    with pytest.raises(LocalControlStoreError, match="invalid local credentials"):
        store.ensure_user("person", "elevenchars", False)
    with pytest.raises(LocalControlStoreError, match="invalid local credentials"):
        store.ensure_user("equalpassword", "EQUALPASSWORD", False)
    equal_user = store.ensure_user("equalpassword", "different valid password", False)
    with pytest.raises(LocalControlStoreError, match="invalid local credentials"):
        store.change_password(
            equal_user.principal.user_id,
            "different valid password",
            "EQUALPASSWORD",
        )
    with pytest.raises(LocalControlStoreNotFoundError, match="local principal not found"):
        store.require_team(token, UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"))


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("missing", "valid password"),
        ("ordinary", "wrong password"),
        ("ordinary", "short"),
        ("ordinary", ""),
    ],
)
def test_authenticate_always_returns_none_for_invalid_supplied_credentials(
    tmp_path: Path,
    username: str,
    password: str,
) -> None:
    store = LocalControlStore(tmp_path)
    store.ensure_user("ordinary", "valid password", False)

    assert store.authenticate(username, password) is None


def test_rejects_boolean_schema_version_even_though_bool_equals_one(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path)
    store.ensure_user("ordinary", "valid password", False)
    document = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    document["schema_version"] = True
    (tmp_path / "control.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LocalControlStoreError, match="invalid local control state"):
        LocalControlStore(tmp_path).authenticate("ordinary", "valid password")
