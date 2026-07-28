import asyncio
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from perfpilot_api.secrets.base import SecretContext, SecretNotFoundError, SecretStoreError
from perfpilot_api.secrets.encrypted_file import EncryptedFileSecretStore
from perfpilot_api.workers import provisioner as provisioner_worker

_TEAM_ID = UUID("20000000-0000-4000-8000-000000000001")
_RESOURCE_ID = UUID("80000000-0000-4000-8000-000000000001")
_OTHER_TEAM_ID = UUID("20000000-0000-4000-8000-000000000002")
_OTHER_RESOURCE_ID = UUID("80000000-0000-4000-8000-000000000002")


def _context(
    *,
    team_id: UUID = _TEAM_ID,
    resource_id: UUID = _RESOURCE_ID,
    credential_version: int = 1,
) -> SecretContext:
    return SecretContext(
        team_id=team_id,
        resource_id=resource_id,
        credential_version=credential_version,
        purpose="tenant_database_password",
    )


def _write_key(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def _secure_directory(path: Path) -> None:
    path.mkdir()
    path.chmod(0o700)


def test_secret_store_persists_owner_only_active_key_generation_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)

    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    manifest = root / ".key-state.json"
    lock_file = root / ".key-state.lock"
    assert json.loads(manifest.read_text()) == {
        "active_key_id": "key-1",
        "generation": 1,
        "retired_key_ids": [],
        "version": 2,
    }
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    store.close()


def test_secret_store_does_not_recreate_a_missing_manifest_for_existing_secrets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    asyncio.run(store.put(b"database-password", context=_context()))
    store.close()
    manifest = root / ".key-state.json"
    manifest.unlink()

    with pytest.raises(SecretStoreError, match="manifest"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )

    assert not manifest.exists()


def test_secret_store_rejects_an_existing_insecure_lock_without_repairing_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    store.close()
    lock_file = root / ".key-state.lock"
    lock_file.chmod(0o666)

    with pytest.raises(SecretStoreError, match="lock.*permissions"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )

    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o666


def test_secret_store_allocates_distinct_opaque_uuid4_references(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    first_reference = store.allocate_reference()
    second_reference = store.allocate_reference()

    assert first_reference.startswith("secret://")
    assert UUID(first_reference.removeprefix("secret://")).version == 4
    assert second_reference.startswith("secret://")
    assert UUID(second_reference.removeprefix("secret://")).version == 4
    assert first_reference != second_reference


@pytest.mark.asyncio
async def test_secret_store_put_retries_at_a_reserved_reference_with_matching_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = store.allocate_reference()

    first_result = await store.put(
        b"first-database-password",
        context=_context(),
        reference=reference,
    )
    second_result = await store.put(
        b"replacement-database-password",
        context=_context(),
        reference=reference,
    )

    assert first_result == reference
    assert second_result == reference
    assert await store.get(reference, context=_context()) == b"replacement-database-password"
    assert len(list(root.glob("*.secret"))) == 1


@pytest.mark.asyncio
async def test_secret_store_put_at_an_existing_reference_rejects_a_different_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = store.allocate_reference()
    await store.put(b"original-database-password", context=_context(), reference=reference)

    with pytest.raises(SecretStoreError, match="authentication"):
        await store.put(
            b"replacement-database-password",
            context=_context(resource_id=_OTHER_RESOURCE_ID),
            reference=reference,
        )

    assert await store.get(reference, context=_context()) == b"original-database-password"


@pytest.mark.asyncio
async def test_secret_store_put_at_an_existing_reference_rejects_a_mismatched_envelope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    first_reference = store.allocate_reference()
    second_reference = store.allocate_reference()
    await store.put(b"first-database-password", context=_context(), reference=first_reference)
    await store.put(b"second-database-password", context=_context(), reference=second_reference)
    first_path = root / f"{first_reference.removeprefix('secret://')}.secret"
    second_path = root / f"{second_reference.removeprefix('secret://')}.secret"
    mismatched_payload = second_path.read_bytes()
    first_path.write_bytes(mismatched_payload)
    first_path.chmod(0o600)

    with pytest.raises(SecretStoreError, match="authentication"):
        await store.put(
            b"replacement-database-password",
            context=_context(),
            reference=first_reference,
        )

    assert first_path.read_bytes() == mismatched_payload
    assert await store.get(second_reference, context=_context()) == b"second-database-password"


@pytest.mark.asyncio
async def test_secret_store_uses_opaque_references_and_never_persists_plaintext(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    plaintext = b"password-visible-marker"
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    reference = await store.put(plaintext, context=_context())

    assert reference.startswith("secret://")
    assert UUID(reference.removeprefix("secret://")).version == 4
    assert str(_TEAM_ID) not in reference
    assert str(_RESOURCE_ID) not in reference
    assert "visible-marker" not in reference
    assert await store.get(reference, context=_context()) == plaintext
    persisted = b"".join(path.read_bytes() for path in root.iterdir())
    assert plaintext not in persisted
    assert b"visible-marker" not in persisted
    assert not list(root.glob("*.tmp"))
    secret_files = list(root.glob("*.secret"))
    assert len(secret_files) == 1
    assert stat.S_IMODE(secret_files[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_secret_store_enforces_owner_only_mode_with_a_restrictive_umask(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    previous_umask = os.umask(0o777)
    try:
        reference = await store.put(b"database-password", context=_context())
    finally:
        os.umask(previous_umask)

    secret_file = next(root.glob("*.secret"))
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert await store.get(reference, context=_context()) == b"database-password"


@pytest.mark.asyncio
async def test_secret_store_fsyncs_file_then_atomically_replaces_then_fsyncs_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    root_fd = store._root_fd  # noqa: SLF001 - verify the durability boundary
    real_fsync = os.fsync
    real_replace = os.replace
    operations: list[str] = []

    def record_fsync(file_descriptor: int) -> None:
        operations.append("fsync_directory" if file_descriptor == root_fd else "fsync_file")
        real_fsync(file_descriptor)

    def record_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd == root_fd
        assert dst_dir_fd == root_fd
        operations.append("replace")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)

    await store.put(b"database-password", context=_context())

    assert operations == ["fsync_file", "replace", "fsync_directory"]


@pytest.mark.asyncio
async def test_interrupted_atomic_replace_retains_the_last_valid_ciphertext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    secret_file = next(root.glob("*.secret"))
    last_valid_payload = secret_file.read_bytes()

    def interrupt_replace(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected atomic replace interruption")

    monkeypatch.setattr(os, "replace", interrupt_replace)
    with pytest.raises(SecretStoreError, match="rotation"):
        await store.rotate(new_key_id="key-2", new_key_file=new_key)

    assert secret_file.read_bytes() == last_valid_payload
    assert not list(root.glob("*.tmp"))
    assert await store.get(reference, context=_context()) == b"database-password"


@pytest.mark.asyncio
async def test_secret_store_rejects_invalid_or_insecure_key_material(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    short_key = tmp_path / "short.key"
    _write_key(short_key, b"x" * 31)

    with pytest.raises(SecretStoreError, match="master key"):
        EncryptedFileSecretStore(
            root,
            key_files={"short": short_key},
            active_key_id="short",
        )

    long_key = tmp_path / "long.key"
    _write_key(long_key, b"x" * 33)
    with pytest.raises(SecretStoreError, match="exactly 32 bytes"):
        EncryptedFileSecretStore(
            root,
            key_files={"long": long_key},
            active_key_id="long",
        )

    insecure_key = tmp_path / "insecure.key"
    _write_key(insecure_key, b"x" * 32)
    insecure_key.chmod(0o640)
    with pytest.raises(SecretStoreError, match="permissions"):
        EncryptedFileSecretStore(
            root,
            key_files={"insecure": insecure_key},
            active_key_id="insecure",
        )


def test_secret_store_accepts_an_owner_read_only_master_key(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    key_file.chmod(0o400)

    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    store.close()


def test_secret_store_rejects_an_insecure_store_mount(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir()
    root.chmod(0o750)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)

    with pytest.raises(SecretStoreError, match="permissions"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )


def test_secret_store_can_close_after_constructor_rejects_the_mount(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    root.mkdir()
    root.chmod(0o750)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore.__new__(EncryptedFileSecretStore)

    with pytest.raises(SecretStoreError, match="permissions"):
        store.__init__(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )

    store.close()


def test_secret_store_closes_a_mount_descriptor_when_validation_rejects_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    root.mkdir()
    root.chmod(0o750)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    real_open = os.open
    real_close = os.close
    mount_descriptors: list[int] = []
    closed_descriptors: list[int] = []

    def record_open(
        path: str | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == root:
            mount_descriptors.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)

    with pytest.raises(SecretStoreError, match="permissions"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )

    assert len(mount_descriptors) == 1
    assert mount_descriptors[0] in closed_descriptors


@pytest.mark.asyncio
async def test_secret_store_rejects_tampering_without_exposing_ciphertext_or_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    secret_file = next(root.glob("*.secret"))
    envelope = json.loads(secret_file.read_text())
    envelope["ciphertext"] = "AAAA"
    secret_file.write_text(json.dumps(envelope))
    secret_file.chmod(0o600)

    with pytest.raises(SecretStoreError) as exc_info:
        await store.get(reference, context=_context())

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert reference not in rendered
    assert "AAAA" not in rendered
    assert "database-password" not in rendered


@pytest.mark.asyncio
async def test_secret_store_delete_is_idempotent_and_missing_get_is_typed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())

    await store.delete(reference)
    await store.delete(reference)

    with pytest.raises(SecretNotFoundError):
        await store.get(reference, context=_context())


@pytest.mark.asyncio
async def test_interrupted_rotation_keeps_old_and_rewritten_secrets_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key},
        active_key_id="key-1",
    )
    references = [
        await store.put(f"secret-{index}".encode(), context=_context()) for index in range(3)
    ]
    real_atomic_write = store._atomic_write  # noqa: SLF001 - deliberate crash injection
    writes = 0

    def fail_during_second_rewrite(path: Path, payload: bytes) -> None:
        nonlocal writes
        if path.suffix != ".secret":
            real_atomic_write(path, payload)
            return
        writes += 1
        if writes == 2:
            raise OSError("injected rotation interruption")
        real_atomic_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", fail_during_second_rewrite)
    with pytest.raises(SecretStoreError, match="rotation"):
        await store.rotate(new_key_id="key-2", new_key_file=new_key)

    assert {json.loads(path.read_text())["key_id"] for path in root.glob("*.secret")} == {
        "key-1",
        "key-2",
    }
    assert [await store.get(reference, context=_context()) for reference in references] == [
        b"secret-0",
        b"secret-1",
        b"secret-2",
    ]

    store.close()
    restarted_store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-2",
    )
    assert [
        await restarted_store.get(reference, context=_context()) for reference in references
    ] == [
        b"secret-0",
        b"secret-1",
        b"secret-2",
    ]
    with pytest.raises(SecretStoreError, match="still protects"):
        await restarted_store.retire_key("key-1")

    await restarted_store.rotate(new_key_id="key-2", new_key_file=new_key)
    assert [
        await restarted_store.get(reference, context=_context()) for reference in references
    ] == [
        b"secret-0",
        b"secret-1",
        b"secret-2",
    ]
    assert {json.loads(path.read_text())["key_id"] for path in root.glob("*.secret")} == {"key-2"}
    await restarted_store.retire_key("key-1")

    restarted_store.close()
    new_key_only_store = EncryptedFileSecretStore(
        root,
        key_files={"key-2": new_key},
        active_key_id="key-2",
    )
    assert [
        await new_key_only_store.get(reference, context=_context()) for reference in references
    ] == [
        b"secret-0",
        b"secret-1",
        b"secret-2",
    ]


@pytest.mark.asyncio
async def test_restart_uses_the_durable_rotated_key_instead_of_stale_configuration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )

    await store.rotate(new_key_id="key-2", new_key_file=new_key)
    store.close()

    assert json.loads((root / ".key-state.json").read_text()) == {
        "active_key_id": "key-2",
        "generation": 2,
        "retired_key_ids": [],
        "version": 2,
    }
    restarted_store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    reference = await restarted_store.put(b"database-password", context=_context())
    envelope = json.loads((root / f"{reference.removeprefix('secret://')}.secret").read_text())
    assert envelope["key_id"] == "key-2"


@pytest.mark.asyncio
async def test_existing_peer_refreshes_the_durable_generation_before_put(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    first_store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    existing_peer = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )

    await first_store.rotate(new_key_id="key-2", new_key_file=new_key)
    reference = await existing_peer.put(b"database-password", context=_context())

    envelope = json.loads((root / f"{reference.removeprefix('secret://')}.secret").read_text())
    assert envelope["key_id"] == "key-2"
    assert json.loads((root / ".key-state.json").read_text())["generation"] == 2


@pytest.mark.asyncio
async def test_existing_peer_refuses_to_retire_the_durable_active_key(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    first_store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    existing_peer = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    await first_store.rotate(new_key_id="key-2", new_key_file=new_key)
    manifest_before_retirement = (root / ".key-state.json").read_bytes()

    with pytest.raises(SecretStoreError, match="active master key"):
        await existing_peer.retire_key("key-2")

    assert (root / ".key-state.json").read_bytes() == manifest_before_retirement


def test_peer_delete_waits_for_an_in_progress_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    first_store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    existing_peer = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    reference = asyncio.run(first_store.put(b"database-password", context=_context()))
    real_atomic_write = first_store._atomic_write  # noqa: SLF001 - lock boundary injection
    real_unlink = os.unlink
    rotation_holds_lock = threading.Event()
    release_rotation = threading.Event()
    delete_started = threading.Event()
    delete_unlinked = threading.Event()

    def pause_secret_rewrite(path: Path, payload: bytes) -> None:
        if path.suffix == ".secret":
            rotation_holds_lock.set()
            if not release_rotation.wait(timeout=5):
                raise OSError("timed out waiting to release rotation")
        real_atomic_write(path, payload)

    def record_unlink(
        path: str | os.PathLike[str],
        *args: object,
        **kwargs: object,
    ) -> None:
        if str(path).endswith(".secret"):
            delete_unlinked.set()
        real_unlink(path, *args, **kwargs)

    def rotate() -> None:
        asyncio.run(first_store.rotate(new_key_id="key-2", new_key_file=new_key))

    def delete() -> None:
        delete_started.set()
        asyncio.run(existing_peer.delete(reference))

    monkeypatch.setattr(first_store, "_atomic_write", pause_secret_rewrite)
    monkeypatch.setattr(os, "unlink", record_unlink)
    with ThreadPoolExecutor(max_workers=2) as executor:
        rotation_future = executor.submit(rotate)
        assert rotation_holds_lock.wait(timeout=5)
        delete_future = executor.submit(delete)
        assert delete_started.wait(timeout=5)
        try:
            assert not delete_unlinked.wait(timeout=0.1)
        finally:
            release_rotation.set()
        rotation_future.result(timeout=5)
        delete_future.result(timeout=5)

    assert delete_unlinked.is_set()


def test_peer_get_waits_for_an_in_progress_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    first_store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    existing_peer = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    reference = asyncio.run(first_store.put(b"database-password", context=_context()))
    real_atomic_write = first_store._atomic_write  # noqa: SLF001 - lock boundary injection
    real_read_file = existing_peer._read_file  # noqa: SLF001 - lock boundary injection
    rotation_holds_lock = threading.Event()
    release_rotation = threading.Event()
    get_started = threading.Event()
    secret_read_started = threading.Event()

    def pause_secret_rewrite(path: Path, payload: bytes) -> None:
        if path.suffix == ".secret":
            rotation_holds_lock.set()
            if not release_rotation.wait(timeout=5):
                raise OSError("timed out waiting to release rotation")
        real_atomic_write(path, payload)

    def record_read(path: Path) -> bytes:
        if path.suffix == ".secret":
            secret_read_started.set()
        return real_read_file(path)

    def rotate() -> None:
        asyncio.run(first_store.rotate(new_key_id="key-2", new_key_file=new_key))

    def get() -> bytes:
        get_started.set()
        return asyncio.run(existing_peer.get(reference, context=_context()))

    monkeypatch.setattr(first_store, "_atomic_write", pause_secret_rewrite)
    monkeypatch.setattr(existing_peer, "_read_file", record_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        rotation_future = executor.submit(rotate)
        assert rotation_holds_lock.wait(timeout=5)
        get_future = executor.submit(get)
        assert get_started.wait(timeout=5)
        try:
            assert not secret_read_started.wait(timeout=0.1)
        finally:
            release_rotation.set()
        rotation_future.result(timeout=5)
    assert get_future.result(timeout=5) == b"database-password"

    assert secret_read_started.is_set()
    assert existing_peer._active_key_id == "key-2"  # noqa: SLF001 - durable refresh evidence
    assert existing_peer._key_generation == 2  # noqa: SLF001 - durable refresh evidence


def test_two_instances_serialize_rotations_and_advance_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    first_key = tmp_path / "first.key"
    second_key = tmp_path / "second.key"
    third_key = tmp_path / "third.key"
    _write_key(first_key, b"1" * 32)
    _write_key(second_key, b"2" * 32)
    _write_key(third_key, b"3" * 32)
    key_files = {
        "key-1": first_key,
        "key-2": second_key,
        "key-3": third_key,
    }
    first_store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    second_store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    asyncio.run(first_store.put(b"database-password", context=_context()))
    first_atomic_write = first_store._atomic_write  # noqa: SLF001 - lock boundary injection
    second_atomic_write = second_store._atomic_write  # noqa: SLF001 - lock boundary injection
    first_rotation_holds_lock = threading.Event()
    release_first_rotation = threading.Event()
    second_rotation_started = threading.Event()
    second_secret_rewrite_started = threading.Event()

    def pause_first_secret_rewrite(path: Path, payload: bytes) -> None:
        if path.suffix == ".secret":
            first_rotation_holds_lock.set()
            if not release_first_rotation.wait(timeout=5):
                raise OSError("timed out waiting to release first rotation")
        first_atomic_write(path, payload)

    def record_second_secret_rewrite(path: Path, payload: bytes) -> None:
        if path.suffix == ".secret":
            second_secret_rewrite_started.set()
        second_atomic_write(path, payload)

    def rotate_to_second_key() -> None:
        asyncio.run(first_store.rotate(new_key_id="key-2", new_key_file=second_key))

    def rotate_to_third_key() -> None:
        second_rotation_started.set()
        asyncio.run(second_store.rotate(new_key_id="key-3", new_key_file=third_key))

    monkeypatch.setattr(first_store, "_atomic_write", pause_first_secret_rewrite)
    monkeypatch.setattr(second_store, "_atomic_write", record_second_secret_rewrite)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(rotate_to_second_key)
        assert first_rotation_holds_lock.wait(timeout=5)
        second_future = executor.submit(rotate_to_third_key)
        assert second_rotation_started.wait(timeout=5)
        try:
            assert not second_secret_rewrite_started.wait(timeout=0.1)
        finally:
            release_first_rotation.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    assert second_secret_rewrite_started.is_set()
    assert json.loads((root / ".key-state.json").read_text()) == {
        "active_key_id": "key-3",
        "generation": 3,
        "retired_key_ids": [],
        "version": 2,
    }
    assert {json.loads(path.read_text())["key_id"] for path in root.glob("*.secret")} == {"key-3"}


def test_v1_key_state_is_atomically_upgraded_with_an_empty_retirement_deny_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    manifest = root / ".key-state.json"
    manifest.write_text(
        json.dumps(
            {
                "active_key_id": "key-1",
                "generation": 7,
                "version": 1,
            }
        )
    )
    manifest.chmod(0o600)
    real_replace = os.replace
    replaced_destinations: list[str] = []

    def record_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replaced_destinations.append(destination)
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", record_replace)

    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    assert json.loads(manifest.read_text()) == {
        "active_key_id": "key-1",
        "generation": 7,
        "retired_key_ids": [],
        "version": 2,
    }
    assert replaced_destinations == [".key-state.json"]
    assert not list(root.glob("*.tmp"))
    store.close()


@pytest.mark.asyncio
async def test_key_retirement_persists_across_peer_refresh_and_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    key_files = {"key-1": old_key, "key-2": new_key}
    retiring_store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    existing_peer = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    reference = await retiring_store.put(b"database-password", context=_context())
    await retiring_store.rotate(new_key_id="key-2", new_key_file=new_key)

    await retiring_store.retire_key("key-1")

    assert json.loads((root / ".key-state.json").read_text()) == {
        "active_key_id": "key-2",
        "generation": 3,
        "retired_key_ids": ["key-1"],
        "version": 2,
    }
    assert "key-1" not in retiring_store._keys  # noqa: SLF001 - deny-list evidence
    assert await existing_peer.get(reference, context=_context()) == b"database-password"
    assert "key-1" not in existing_peer._keys  # noqa: SLF001 - refresh evidence
    retiring_store.close()
    existing_peer.close()

    restarted_store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    assert "key-1" not in restarted_store._keys  # noqa: SLF001 - restart evidence
    assert await restarted_store.get(reference, context=_context()) == b"database-password"


@pytest.mark.asyncio
async def test_retired_key_id_cannot_be_rotated_back_even_with_the_same_key_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key, "key-2": new_key},
        active_key_id="key-1",
    )
    await store.rotate(new_key_id="key-2", new_key_file=new_key)
    await store.retire_key("key-1")

    with pytest.raises(SecretStoreError, match="retired"):
        await store.rotate(new_key_id="key-1", new_key_file=old_key)

    assert json.loads((root / ".key-state.json").read_text()) == {
        "active_key_id": "key-2",
        "generation": 3,
        "retired_key_ids": ["key-1"],
        "version": 2,
    }


@pytest.mark.asyncio
async def test_peer_rotate_refreshes_retirement_before_checking_key_material(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    replacement_old_key = tmp_path / "replacement-old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(replacement_old_key, b"r" * 32)
    _write_key(new_key, b"n" * 32)
    key_files = {"key-1": old_key, "key-2": new_key}
    retiring_store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    stale_peer = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    await retiring_store.rotate(new_key_id="key-2", new_key_file=new_key)
    await retiring_store.retire_key("key-1")

    with pytest.raises(SecretStoreError, match="retired"):
        await stale_peer.rotate(
            new_key_id="key-1",
            new_key_file=replacement_old_key,
        )

    assert "key-1" not in stale_peer._keys  # noqa: SLF001 - refresh-before-validation evidence


@pytest.mark.asyncio
async def test_retirement_manifest_commit_survives_an_interruption_after_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    key_files = {"key-1": old_key, "key-2": new_key}
    store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    await store.rotate(new_key_id="key-2", new_key_file=new_key)
    real_atomic_write = store._atomic_write  # noqa: SLF001 - crash-boundary injection

    def persist_then_interrupt(path: Path, payload: bytes) -> None:
        real_atomic_write(path, payload)
        if path.name == ".key-state.json":
            raise OSError("injected interruption after manifest replacement")

    monkeypatch.setattr(store, "_atomic_write", persist_then_interrupt)

    with pytest.raises(SecretStoreError, match="retirement"):
        await store.retire_key("key-1")

    assert json.loads((root / ".key-state.json").read_text()) == {
        "active_key_id": "key-2",
        "generation": 3,
        "retired_key_ids": ["key-1"],
        "version": 2,
    }
    assert await store.get(reference, context=_context()) == b"database-password"
    assert "key-1" not in store._keys  # noqa: SLF001 - refresh after uncertain commit
    store.close()
    restarted_store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    assert "key-1" not in restarted_store._keys  # noqa: SLF001 - crash recovery evidence


@pytest.mark.asyncio
async def test_interrupted_retirement_replace_leaves_the_previous_manifest_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    key_files = {"key-1": old_key, "key-2": new_key}
    store = EncryptedFileSecretStore(
        root,
        key_files=key_files,
        active_key_id="key-1",
    )
    await store.rotate(new_key_id="key-2", new_key_file=new_key)
    manifest = root / ".key-state.json"
    last_valid_manifest = manifest.read_bytes()
    real_replace = os.replace

    def interrupt_manifest_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if destination == ".key-state.json":
            raise OSError("injected interruption before manifest replacement")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    with monkeypatch.context() as interruption:
        interruption.setattr(os, "replace", interrupt_manifest_replace)
        with pytest.raises(SecretStoreError, match="retirement"):
            await store.retire_key("key-1")

    assert manifest.read_bytes() == last_valid_manifest
    assert json.loads(last_valid_manifest)["retired_key_ids"] == []
    assert "key-1" in store._keys  # noqa: SLF001 - retirement did not commit
    assert not list(root.glob("*.tmp"))


def test_secret_maintenance_cli_persists_retirement_for_a_real_store_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "secrets"
    _secure_directory(store_root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    keyring_config = tmp_path / "keyring.json"
    keyring_config.write_text(
        json.dumps(
            {
                "active_key_id": "key-1",
                "keys": {"key-1": str(old_key), "key-2": str(new_key)},
            }
        )
    )
    keyring_config.chmod(0o600)
    monkeypatch.setattr(
        provisioner_worker,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    monkeypatch.setenv("PERFPILOT_SECRET_KEYRING_CONFIG", str(keyring_config))
    monkeypatch.setenv("PERFPILOT_SECRET_STORE_ROOT", str(store_root))

    provisioner_worker.secret_maintenance_main(
        ["rotate", "--key-id", "key-2", "--key-file", str(new_key)]
    )
    provisioner_worker.secret_maintenance_main(["retire", "--key-id", "key-1"])

    restarted_store = provisioner_worker._build_configured_secret_store()
    try:
        assert "key-1" not in restarted_store._keys  # noqa: SLF001 - real restart evidence
        with pytest.raises(SecretStoreError, match="retired"):
            asyncio.run(restarted_store.rotate(new_key_id="key-1", new_key_file=old_key))
    finally:
        restarted_store.close()


@pytest.mark.asyncio
async def test_key_retirement_authenticates_every_envelope_before_removing_a_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    old_key = tmp_path / "old.key"
    new_key = tmp_path / "new.key"
    _write_key(old_key, b"o" * 32)
    _write_key(new_key, b"n" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": old_key},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())

    def interrupt_before_rewrite(path: Path, payload: bytes) -> None:
        del path, payload
        raise OSError("injected rotation interruption")

    monkeypatch.setattr(store, "_atomic_write", interrupt_before_rewrite)
    with pytest.raises(SecretStoreError, match="rotation"):
        await store.rotate(new_key_id="key-2", new_key_file=new_key)

    secret_file = next(root.glob("*.secret"))
    original_payload = secret_file.read_bytes()
    corrupted_envelope = json.loads(original_payload)
    corrupted_envelope["key_id"] = "key-2"
    secret_file.write_text(json.dumps(corrupted_envelope))
    secret_file.chmod(0o600)

    with pytest.raises(SecretStoreError):
        await store.retire_key("key-1")

    secret_file.write_bytes(original_payload)
    secret_file.chmod(0o600)
    assert await store.get(reference, context=_context()) == b"database-password"


@pytest.mark.asyncio
async def test_secret_references_cannot_escape_the_store_root(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    for reference in ("../master.key", "secret://../master", "secret:///absolute"):
        with pytest.raises(SecretStoreError):
            await store.get(reference, context=_context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement", "replacement_context"),
    [
        ("team_id", str(_OTHER_TEAM_ID), _context(team_id=_OTHER_TEAM_ID)),
        (
            "resource_id",
            str(_OTHER_RESOURCE_ID),
            _context(resource_id=_OTHER_RESOURCE_ID),
        ),
        ("credential_version", 2, _context(credential_version=2)),
        ("purpose", "unsupported-purpose", _context()),
    ],
)
async def test_secret_ciphertext_is_bound_to_its_routing_context(
    tmp_path: Path,
    field: str,
    replacement: str | int,
    replacement_context: SecretContext,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    secret_file = next(root.glob("*.secret"))
    envelope = json.loads(secret_file.read_text())
    envelope["context"][field] = replacement
    secret_file.write_text(json.dumps(envelope))
    secret_file.chmod(0o600)

    with pytest.raises(SecretStoreError):
        await store.get(reference, context=replacement_context)


@pytest.mark.asyncio
async def test_secret_ciphertext_aad_binds_the_key_identifier(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    first_key = tmp_path / "first.key"
    second_key = tmp_path / "second.key"
    _write_key(first_key, b"k" * 32)
    _write_key(second_key, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": first_key, "key-2": second_key},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    secret_file = next(root.glob("*.secret"))
    envelope = json.loads(secret_file.read_text())
    envelope["key_id"] = "key-2"
    envelope["context"]["key_id"] = "key-2"
    secret_file.write_text(json.dumps(envelope))
    secret_file.chmod(0o600)

    with pytest.raises(SecretStoreError, match="authentication"):
        await store.get(reference, context=_context())


@pytest.mark.asyncio
async def test_secret_ciphertext_is_bound_to_its_opaque_reference(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    first_reference = await store.put(b"first-password", context=_context())
    second_reference = await store.put(b"second-password", context=_context())
    first_id = first_reference.removeprefix("secret://")
    second_id = second_reference.removeprefix("secret://")
    first_path = root / f"{first_id}.secret"
    second_path = root / f"{second_id}.secret"
    first_payload = first_path.read_bytes()
    second_payload = second_path.read_bytes()
    first_path.write_bytes(second_payload)
    second_path.write_bytes(first_payload)
    first_path.chmod(0o600)
    second_path.chmod(0o600)

    with pytest.raises(SecretStoreError, match="authentication"):
        await store.get(first_reference, context=_context())
    with pytest.raises(SecretStoreError, match="authentication"):
        await store.get(second_reference, context=_context())


def test_secret_store_does_not_follow_a_master_key_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is unavailable")
    root = tmp_path / "secrets"
    _secure_directory(root)
    target_key = tmp_path / "target.key"
    _write_key(target_key, b"k" * 32)
    key_link = tmp_path / "master.key"
    key_link.symlink_to(target_key)

    with pytest.raises(SecretStoreError, match="master key"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_link},
            active_key_id="key-1",
        )


def test_secret_store_fails_closed_without_nofollow_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("host already lacks O_NOFOLLOW")
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(SecretStoreError, match="secure filesystem"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )


def test_secret_store_does_not_follow_a_store_mount_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is unavailable")
    target_root = tmp_path / "target-secrets"
    _secure_directory(target_root)
    root_link = tmp_path / "secrets"
    root_link.symlink_to(target_root, target_is_directory=True)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)

    with pytest.raises(SecretStoreError, match="mount"):
        EncryptedFileSecretStore(
            root_link,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )


def test_secret_store_does_not_chmod_a_replaced_mount_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is unavailable")
    root = tmp_path / "secrets"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o750)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    real_chmod = Path.chmod

    def replace_mount_before_path_chmod(
        path: Path,
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        if path == root:
            path.rmdir()
            path.symlink_to(victim, target_is_directory=True)
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", replace_mount_before_path_chmod)

    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )

    assert stat.S_IMODE(victim.stat().st_mode) == 0o750
    store.close()


@pytest.mark.asyncio
async def test_secret_store_does_not_follow_an_encrypted_secret_symlink(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is unavailable")
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    secret_file = next(root.glob("*.secret"))
    outside_file = tmp_path / "outside.secret"
    outside_file.write_bytes(secret_file.read_bytes())
    outside_file.chmod(0o600)
    secret_file.unlink()
    secret_file.symlink_to(outside_file)

    with pytest.raises(SecretStoreError, match="unavailable"):
        await store.get(reference, context=_context())


@pytest.mark.asyncio
async def test_secret_store_rejects_non_owner_only_encrypted_secret_permissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    next(root.glob("*.secret")).chmod(0o640)

    with pytest.raises(SecretStoreError, match="permissions"):
        await store.get(reference, context=_context())


@pytest.mark.asyncio
async def test_secret_store_accepts_an_owner_read_only_encrypted_secret(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    next(root.glob("*.secret")).chmod(0o400)

    assert await store.get(reference, context=_context()) == b"database-password"


@pytest.mark.asyncio
async def test_secret_store_rejects_a_non_regular_encrypted_secret(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    _write_key(key_file, b"k" * 32)
    store = EncryptedFileSecretStore(
        root,
        key_files={"key-1": key_file},
        active_key_id="key-1",
    )
    reference = await store.put(b"database-password", context=_context())
    secret_path = next(root.glob("*.secret"))
    secret_path.unlink()
    secret_path.mkdir(mode=0o700)

    with pytest.raises(SecretStoreError, match="regular file"):
        await store.get(reference, context=_context())


def test_secret_store_requires_owner_owned_regular_key_file(tmp_path: Path) -> None:
    if not hasattr(os, "geteuid"):
        pytest.skip("owner validation requires POSIX")
    root = tmp_path / "secrets"
    _secure_directory(root)
    key_file = tmp_path / "master.key"
    key_file.mkdir()
    key_file.chmod(0o600)

    with pytest.raises(SecretStoreError, match="regular file"):
        EncryptedFileSecretStore(
            root,
            key_files={"key-1": key_file},
            active_key_id="key-1",
        )
