import base64
import errno
import fcntl
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from perfpilot_api.secrets.base import SecretContext, SecretNotFoundError, SecretStoreError

_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_MAX_SECRET_BYTES = 64 * 1024
_ENVELOPE_VERSION = 1
_FILE_SUFFIX = ".secret"
_REFERENCE_PREFIX = "secret://"
_KEY_STATE_VERSION = 2
_LEGACY_KEY_STATE_VERSION = 1
_KEY_STATE_FILE = ".key-state.json"
_KEY_STATE_LOCK_FILE = ".key-state.lock"
_OWNER_ONLY_FILE_MODE = 0o600
_OWNER_ONLY_DIRECTORY_MODE = 0o700
_OWNER_ONLY_READABLE_FILE_MODES = frozenset({0o400, _OWNER_ONLY_FILE_MODE})
_REQUIRED_OS_PRIMITIVES = ("O_DIRECTORY", "O_NOFOLLOW", "fchmod", "geteuid")


def _redacted_error(message: str) -> SecretStoreError:
    return SecretStoreError(message)


class _RetiredKeyError(SecretStoreError):
    pass


class _KeyMaterialConflictError(SecretStoreError):
    pass


def _validate_key_id(key_id: str) -> None:
    if not _KEY_ID_PATTERN.fullmatch(key_id):
        raise _redacted_error("invalid master key identifier")


def _require_secure_filesystem_primitives() -> None:
    if any(not hasattr(os, name) for name in _REQUIRED_OS_PRIMITIVES):
        raise _redacted_error("secure filesystem primitives are unavailable")


def _require_owner(stat_result: os.stat_result, *, subject: str) -> None:
    if hasattr(os, "geteuid") and stat_result.st_uid != os.geteuid():
        raise _redacted_error(f"{subject} must be owned by the service account")


def _require_owner_only_permissions(
    stat_result: os.stat_result,
    *,
    allowed_modes: frozenset[int],
    subject: str,
) -> None:
    if stat.S_IMODE(stat_result.st_mode) not in allowed_modes:
        raise _redacted_error(f"{subject} permissions must be owner-only")


def _read_all(file_descriptor: int, *, maximum: int | None = None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if maximum is not None and total > maximum:
            raise _redacted_error("encrypted secret is too large")


def _write_all(file_descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise OSError("short encrypted-secret write")
        view = view[written:]


class EncryptedFileSecretStore:
    """An owner-only AES-GCM store addressed exclusively by opaque UUID references."""

    def __init__(
        self,
        root: Path,
        *,
        key_files: Mapping[str, Path],
        active_key_id: str,
    ) -> None:
        _require_secure_filesystem_primitives()
        self._root = root.resolve(strict=False)
        self._root_fd = -1
        self._root_fd = self._open_store_root(root)
        self._keys: dict[str, bytes] = {}
        self._retired_key_ids: frozenset[str] = frozenset()
        try:
            for key_id, key_file in key_files.items():
                _validate_key_id(key_id)
                self._keys[key_id] = self._load_master_key(key_file)
            with self._store_lock():
                key_state = self._load_key_state()
                if key_state is None:
                    if active_key_id not in self._keys:
                        raise _redacted_error("active master key is unavailable")
                    try:
                        has_existing_secrets = any(
                            name.endswith(_FILE_SUFFIX) for name in os.listdir(self._root_fd)
                        )
                    except OSError as exc:
                        raise _redacted_error("key-state manifest bootstrap failed") from exc
                    if has_existing_secrets:
                        raise _redacted_error(
                            "key-state manifest is unavailable for existing secrets"
                        )
                    key_state = (active_key_id, 1, frozenset(), False)
                    self._persist_key_state(
                        active_key_id=active_key_id,
                        generation=1,
                        retired_key_ids=frozenset(),
                    )
                durable_active_key_id, generation, retired_key_ids, needs_upgrade = key_state
                if needs_upgrade:
                    self._persist_key_state(
                        active_key_id=durable_active_key_id,
                        generation=generation,
                        retired_key_ids=retired_key_ids,
                    )
                self._apply_key_state(
                    active_key_id=durable_active_key_id,
                    generation=generation,
                    retired_key_ids=retired_key_ids,
                )
        except BaseException:
            os.close(self._root_fd)
            self._root_fd = -1
            raise

    @staticmethod
    def _open_store_root(root: Path) -> int:
        descriptor = -1
        try:
            if not root.exists():
                root.mkdir(mode=_OWNER_ONLY_DIRECTORY_MODE, parents=False)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(root, flags)
            root_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise _redacted_error("encrypted store mount must be a directory")
            _require_owner(root_stat, subject="encrypted store mount")
            _require_owner_only_permissions(
                root_stat,
                allowed_modes=frozenset({_OWNER_ONLY_DIRECTORY_MODE}),
                subject="encrypted store mount",
            )
            return descriptor
        except SecretStoreError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise _redacted_error("encrypted store mount is unavailable") from exc

    @staticmethod
    def _load_master_key(key_file: Path) -> bytes:
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(key_file, flags)
            key_stat = os.fstat(descriptor)
            if not stat.S_ISREG(key_stat.st_mode):
                raise _redacted_error("master key must be a regular file")
            _require_owner(key_stat, subject="master key")
            _require_owner_only_permissions(
                key_stat,
                allowed_modes=_OWNER_ONLY_READABLE_FILE_MODES,
                subject="master key",
            )
            try:
                key = _read_all(descriptor, maximum=32)
            except SecretStoreError:
                raise _redacted_error("master key must contain exactly 32 bytes") from None
            if len(key) != 32:
                raise _redacted_error("master key must contain exactly 32 bytes")
            return key
        except SecretStoreError:
            raise
        except OSError as exc:
            raise _redacted_error("master key is unavailable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @contextmanager
    def _store_lock(self) -> Iterator[None]:
        descriptor = -1
        created = False
        try:
            try:
                descriptor = os.open(
                    _KEY_STATE_LOCK_FILE,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    _OWNER_ONLY_FILE_MODE,
                    dir_fd=self._root_fd,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    _KEY_STATE_LOCK_FILE,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise _redacted_error("key-state lock must be a regular file")
            _require_owner(lock_stat, subject="key-state lock")
            if created:
                os.fchmod(descriptor, _OWNER_ONLY_FILE_MODE)
            else:
                _require_owner_only_permissions(
                    lock_stat,
                    allowed_modes=_OWNER_ONLY_READABLE_FILE_MODES,
                    subject="key-state lock",
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except SecretStoreError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise _redacted_error("key-state lock is unavailable") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _load_key_state(self) -> tuple[str, int, frozenset[str], bool] | None:
        try:
            payload = self._read_file(self._root / _KEY_STATE_FILE)
        except SecretNotFoundError:
            return None
        try:
            state = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _redacted_error("key-state manifest is invalid") from None
        if not isinstance(state, dict):
            raise _redacted_error("key-state manifest is invalid")
        version = state.get("version")
        if version == _LEGACY_KEY_STATE_VERSION:
            expected_fields = {"active_key_id", "generation", "version"}
            retired_key_ids: list[str] = []
            needs_upgrade = True
        elif version == _KEY_STATE_VERSION:
            expected_fields = {
                "active_key_id",
                "generation",
                "retired_key_ids",
                "version",
            }
            raw_retired_key_ids = state.get("retired_key_ids")
            if not isinstance(raw_retired_key_ids, list) or any(
                not isinstance(key_id, str) for key_id in raw_retired_key_ids
            ):
                raise _redacted_error("key-state manifest is invalid")
            retired_key_ids = raw_retired_key_ids
            if retired_key_ids != sorted(set(retired_key_ids)):
                raise _redacted_error("key-state manifest is invalid")
            needs_upgrade = False
        else:
            raise _redacted_error("key-state manifest is invalid")
        active_key_id = state.get("active_key_id")
        generation = state.get("generation")
        if (
            set(state) != expected_fields
            or not isinstance(active_key_id, str)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise _redacted_error("key-state manifest is invalid")
        _validate_key_id(active_key_id)
        for retired_key_id in retired_key_ids:
            _validate_key_id(retired_key_id)
        retired = frozenset(retired_key_ids)
        if active_key_id in retired:
            raise _redacted_error("key-state manifest is invalid")
        return active_key_id, generation, retired, needs_upgrade

    def _persist_key_state(
        self,
        *,
        active_key_id: str,
        generation: int,
        retired_key_ids: frozenset[str],
    ) -> None:
        payload = json.dumps(
            {
                "active_key_id": active_key_id,
                "generation": generation,
                "retired_key_ids": sorted(retired_key_ids),
                "version": _KEY_STATE_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self._atomic_write(self._root / _KEY_STATE_FILE, payload)
        except OSError as exc:
            raise _redacted_error("key-state manifest update failed") from exc

    def _apply_key_state(
        self,
        *,
        active_key_id: str,
        generation: int,
        retired_key_ids: frozenset[str],
    ) -> None:
        for retired_key_id in retired_key_ids:
            self._keys.pop(retired_key_id, None)
        if active_key_id not in self._keys:
            raise _redacted_error("durable active master key is unavailable")
        self._active_key_id = active_key_id
        self._key_generation = generation
        self._retired_key_ids = retired_key_ids

    def _refresh_key_state_locked(self) -> None:
        key_state = self._load_key_state()
        if key_state is None:
            raise _redacted_error("key-state manifest is unavailable")
        active_key_id, generation, retired_key_ids, needs_upgrade = key_state
        if needs_upgrade:
            self._persist_key_state(
                active_key_id=active_key_id,
                generation=generation,
                retired_key_ids=retired_key_ids,
            )
        self._apply_key_state(
            active_key_id=active_key_id,
            generation=generation,
            retired_key_ids=retired_key_ids,
        )

    @staticmethod
    def _context_payload(
        *,
        secret_id: UUID,
        context: SecretContext,
        key_id: str,
    ) -> dict[str, str | int]:
        return {
            "credential_version": context.credential_version,
            "key_id": key_id,
            "purpose": context.purpose,
            "resource_id": str(context.resource_id),
            "secret_id": str(secret_id),
            "team_id": str(context.team_id),
        }

    @staticmethod
    def _aad(payload: Mapping[str, str | int]) -> bytes:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _encode_envelope(
        *,
        secret_id: UUID,
        context: SecretContext,
        key_id: str,
        key: bytes,
        plaintext: bytes,
    ) -> bytes:
        context_payload = EncryptedFileSecretStore._context_payload(
            secret_id=secret_id,
            context=context,
            key_id=key_id,
        )
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            plaintext,
            EncryptedFileSecretStore._aad(context_payload),
        )
        envelope = {
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "context": context_payload,
            "key_id": key_id,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "version": _ENVELOPE_VERSION,
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _parse_reference(reference: str) -> UUID:
        if not isinstance(reference, str) or not reference.startswith(_REFERENCE_PREFIX):
            raise _redacted_error("invalid opaque secret reference")
        raw_identifier = reference.removeprefix(_REFERENCE_PREFIX)
        try:
            identifier = UUID(raw_identifier)
        except (ValueError, AttributeError):
            raise _redacted_error("invalid opaque secret reference") from None
        if str(identifier) != raw_identifier:
            raise _redacted_error("invalid opaque secret reference")
        return identifier

    @staticmethod
    def _decode_context(raw_context: Any) -> tuple[UUID, SecretContext, str]:
        if not isinstance(raw_context, dict):
            raise _redacted_error("encrypted secret envelope is invalid")
        expected_fields = {
            "credential_version",
            "key_id",
            "purpose",
            "resource_id",
            "secret_id",
            "team_id",
        }
        if set(raw_context) != expected_fields:
            raise _redacted_error("encrypted secret envelope is invalid")
        key_id = raw_context["key_id"]
        if not isinstance(key_id, str):
            raise _redacted_error("encrypted secret envelope is invalid")
        _validate_key_id(key_id)
        try:
            context = SecretContext(
                team_id=UUID(str(raw_context["team_id"])),
                resource_id=UUID(str(raw_context["resource_id"])),
                credential_version=int(raw_context["credential_version"]),
                purpose=str(raw_context["purpose"]),  # type: ignore[arg-type]
            )
            secret_id = UUID(str(raw_context["secret_id"]))
        except (ValueError, TypeError):
            raise _redacted_error("encrypted secret envelope is invalid") from None
        return secret_id, context, key_id

    def _read_file(self, path: Path) -> bytes:
        descriptor = -1
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise _redacted_error("encrypted secret must be a regular file")
            _require_owner(file_stat, subject="encrypted secret")
            _require_owner_only_permissions(
                file_stat,
                allowed_modes=_OWNER_ONLY_READABLE_FILE_MODES,
                subject="encrypted secret",
            )
            return _read_all(descriptor, maximum=4 * _MAX_SECRET_BYTES)
        except FileNotFoundError:
            raise SecretNotFoundError("encrypted secret was not found") from None
        except SecretStoreError:
            raise
        except OSError as exc:
            raise _redacted_error("encrypted secret is unavailable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary_name = f".{uuid4()}.tmp"
        descriptor = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                temporary_name,
                flags,
                _OWNER_ONLY_FILE_MODE,
                dir_fd=self._root_fd,
            )
            os.fchmod(descriptor, _OWNER_ONLY_FILE_MODE)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            os.fsync(self._root_fd)
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=self._root_fd)
            except OSError as cleanup_error:
                if cleanup_error.errno != errno.ENOENT:
                    pass
            raise

    @staticmethod
    def _load_envelope(payload: bytes) -> dict[str, Any]:
        try:
            envelope = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _redacted_error("encrypted secret envelope is invalid") from None
        if not isinstance(envelope, dict) or set(envelope) != {
            "ciphertext",
            "context",
            "key_id",
            "nonce",
            "version",
        }:
            raise _redacted_error("encrypted secret envelope is invalid")
        if envelope["version"] != _ENVELOPE_VERSION:
            raise _redacted_error("encrypted secret envelope version is unsupported")
        return envelope

    def _decrypt_envelope(
        self,
        *,
        reference_id: UUID,
        envelope: Mapping[str, Any],
        expected_context: SecretContext | None,
    ) -> tuple[bytes, SecretContext, str]:
        try:
            secret_id, stored_context, context_key_id = self._decode_context(envelope["context"])
            envelope_key_id = envelope["key_id"]
            if (
                secret_id != reference_id
                or not isinstance(envelope_key_id, str)
                or envelope_key_id != context_key_id
                or (expected_context is not None and stored_context != expected_context)
            ):
                raise _redacted_error("encrypted secret authentication failed")
            key = self._keys[envelope_key_id]
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            if len(nonce) != 12:
                raise ValueError
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._aad(envelope["context"]),
            )
            return plaintext, stored_context, envelope_key_id
        except SecretStoreError:
            raise
        except (InvalidTag, KeyError, TypeError, ValueError):
            raise _redacted_error("encrypted secret authentication failed") from None

    def allocate_reference(self) -> str:
        return f"{_REFERENCE_PREFIX}{uuid4()}"

    async def put(
        self,
        secret: bytes,
        *,
        context: SecretContext,
        reference: str | None = None,
    ) -> str:
        if not isinstance(secret, bytes) or not secret or len(secret) > _MAX_SECRET_BYTES:
            raise _redacted_error("secret value is invalid")
        resolved_reference = reference if reference is not None else self.allocate_reference()
        secret_id = self._parse_reference(resolved_reference)
        path = self._root / f"{secret_id}{_FILE_SUFFIX}"
        with self._store_lock():
            self._refresh_key_state_locked()
            if reference is not None:
                try:
                    existing_payload = self._read_file(path)
                except SecretNotFoundError:
                    pass
                else:
                    existing_envelope = self._load_envelope(existing_payload)
                    self._decrypt_envelope(
                        reference_id=secret_id,
                        envelope=existing_envelope,
                        expected_context=context,
                    )
            key_id = self._active_key_id
            payload = self._encode_envelope(
                secret_id=secret_id,
                context=context,
                key_id=key_id,
                key=self._keys[key_id],
                plaintext=secret,
            )
            try:
                self._atomic_write(path, payload)
            except OSError as exc:
                raise _redacted_error("encrypted secret write failed") from exc
        return resolved_reference

    async def get(self, reference: str, *, context: SecretContext) -> bytes:
        secret_id = self._parse_reference(reference)
        with self._store_lock():
            self._refresh_key_state_locked()
            payload = self._read_file(self._root / f"{secret_id}{_FILE_SUFFIX}")
            envelope = self._load_envelope(payload)
            plaintext, _, _ = self._decrypt_envelope(
                reference_id=secret_id,
                envelope=envelope,
                expected_context=context,
            )
        return plaintext

    async def delete(self, reference: str) -> None:
        secret_id = self._parse_reference(reference)
        with self._store_lock():
            self._refresh_key_state_locked()
            try:
                os.unlink(f"{secret_id}{_FILE_SUFFIX}", dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _redacted_error("encrypted secret delete failed") from exc

    async def rotate(self, *, new_key_id: str, new_key_file: Path) -> None:
        _validate_key_id(new_key_id)
        new_key = self._load_master_key(new_key_file)
        try:
            with self._store_lock():
                key_state = self._load_key_state()
                if key_state is None:
                    raise _redacted_error("key-state manifest is unavailable")
                durable_active_key_id, generation, retired_key_ids, needs_upgrade = key_state
                if needs_upgrade:
                    self._persist_key_state(
                        active_key_id=durable_active_key_id,
                        generation=generation,
                        retired_key_ids=retired_key_ids,
                    )
                if new_key_id in retired_key_ids:
                    self._apply_key_state(
                        active_key_id=durable_active_key_id,
                        generation=generation,
                        retired_key_ids=retired_key_ids,
                    )
                    raise _RetiredKeyError("retired master key identifier cannot be reused")
                existing = self._keys.get(new_key_id)
                if existing is not None and existing != new_key:
                    self._apply_key_state(
                        active_key_id=durable_active_key_id,
                        generation=generation,
                        retired_key_ids=retired_key_ids,
                    )
                    raise _KeyMaterialConflictError(
                        "master key identifier already has different material"
                    )
                self._keys[new_key_id] = new_key
                self._apply_key_state(
                    active_key_id=durable_active_key_id,
                    generation=generation,
                    retired_key_ids=retired_key_ids,
                )
                if durable_active_key_id != new_key_id:
                    generation += 1
                    self._persist_key_state(
                        active_key_id=new_key_id,
                        generation=generation,
                        retired_key_ids=retired_key_ids,
                    )
                self._active_key_id = new_key_id
                self._key_generation = generation
                names = sorted(
                    name for name in os.listdir(self._root_fd) if name.endswith(_FILE_SUFFIX)
                )
                for name in names:
                    path = self._root / name
                    secret_id = UUID(name.removesuffix(_FILE_SUFFIX))
                    envelope = self._load_envelope(self._read_file(path))
                    plaintext, context, current_key_id = self._decrypt_envelope(
                        reference_id=secret_id,
                        envelope=envelope,
                        expected_context=None,
                    )
                    if current_key_id == new_key_id:
                        continue
                    replacement = self._encode_envelope(
                        secret_id=secret_id,
                        context=context,
                        key_id=new_key_id,
                        key=new_key,
                        plaintext=plaintext,
                    )
                    self._atomic_write(path, replacement)
        except (_KeyMaterialConflictError, _RetiredKeyError):
            raise
        except (OSError, SecretStoreError, ValueError) as exc:
            raise _redacted_error("master key rotation was interrupted") from exc

    async def retire_key(self, key_id: str) -> None:
        _validate_key_id(key_id)
        try:
            with self._store_lock():
                self._refresh_key_state_locked()
                if key_id == self._active_key_id:
                    raise _redacted_error("active master key cannot be retired")
                if key_id in self._retired_key_ids:
                    return
                for name in os.listdir(self._root_fd):
                    if not name.endswith(_FILE_SUFFIX):
                        continue
                    raw_secret_id = name.removesuffix(_FILE_SUFFIX)
                    try:
                        secret_id = UUID(raw_secret_id)
                    except ValueError:
                        raise _redacted_error("master key retirement failed") from None
                    if str(secret_id) != raw_secret_id:
                        raise _redacted_error("master key retirement failed")
                    envelope = self._load_envelope(self._read_file(self._root / name))
                    _, _, authenticated_key_id = self._decrypt_envelope(
                        reference_id=secret_id,
                        envelope=envelope,
                        expected_context=None,
                    )
                    if authenticated_key_id == key_id:
                        raise _redacted_error("master key still protects stored secrets")
                retired_key_ids = self._retired_key_ids | {key_id}
                try:
                    self._persist_key_state(
                        active_key_id=self._active_key_id,
                        generation=self._key_generation + 1,
                        retired_key_ids=retired_key_ids,
                    )
                except SecretStoreError as exc:
                    raise _redacted_error("master key retirement failed") from exc
                self._retired_key_ids = retired_key_ids
                self._key_generation += 1
                self._keys.pop(key_id, None)
        except OSError as exc:
            raise _redacted_error("master key retirement failed") from exc

    def close(self) -> None:
        root_fd = getattr(self, "_root_fd", -1)
        if root_fd >= 0:
            os.close(root_fd)
            self._root_fd = -1

    def __del__(self) -> None:
        self.close()
