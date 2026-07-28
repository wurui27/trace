import json
import os
import stat
from pathlib import Path

from perfpilot_api.secrets.base import SecretStoreError
from perfpilot_api.secrets.encrypted_file import EncryptedFileSecretStore


_MAX_CONFIG_BYTES = 1024 * 1024


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError
        payload[key] = value
    return payload


def _is_unambiguous_absolute_path(value: str) -> bool:
    path = Path(value)
    return (
        path.is_absolute()
        and path != Path(path.anchor)
        and not value.startswith("//")
        and path.as_posix() == value
        and ".." not in path.parts
    )


def read_owner_only_file(path: Path) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or (hasattr(os, "geteuid") and details.st_uid != os.geteuid())
            or stat.S_IMODE(details.st_mode) not in {0o400, 0o600}
        ):
            raise RuntimeError("runtime secret file permissions are invalid")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_CONFIG_BYTES:
                raise RuntimeError("runtime secret file is too large")
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("runtime secret file could not be read") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_configured_secret_store(
    *,
    keyring_config: Path,
    secret_store_root: Path,
) -> EncryptedFileSecretStore:
    invalid = False
    active_key_id: object = None
    raw_keys: object = None
    try:
        payload = json.loads(
            read_owner_only_file(keyring_config),
            object_pairs_hook=_object_without_duplicate_keys,
        )
        active_key_id = payload["active_key_id"]
        raw_keys = payload["keys"]
        if (
            not isinstance(active_key_id, str)
            or not active_key_id
            or not isinstance(raw_keys, dict)
            or not raw_keys
            or any(
                not isinstance(key_id, str)
                or not key_id
                or not isinstance(key_path, str)
                or not key_path
                or not _is_unambiguous_absolute_path(key_path)
                for key_id, key_path in raw_keys.items()
            )
        ):
            invalid = True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        invalid = True
    if invalid or not isinstance(active_key_id, str) or not isinstance(raw_keys, dict):
        raise RuntimeError("secret keyring configuration is invalid")

    key_files = {key_id: Path(key_path) for key_id, key_path in raw_keys.items()}
    failure_message: str | None = None
    store: EncryptedFileSecretStore | None = None
    try:
        store = EncryptedFileSecretStore(
            secret_store_root,
            key_files=key_files,
            active_key_id=active_key_id,
        )
    except SecretStoreError as error:
        failure_message = str(error)
    except Exception:
        failure_message = "secret store configuration is invalid"
    if store is None:
        raise RuntimeError(failure_message or "secret store configuration is invalid")
    return store


__all__ = ["build_configured_secret_store", "read_owner_only_file"]
