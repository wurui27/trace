from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from perfpilot_agent.credentials import CredentialBackendError

DEFAULT_CREDENTIAL_PATH = Path("/var/lib/perfpilot-agent/credentials.json")
_MAXIMUM_CREDENTIAL_BYTES = 64 * 1024


class LinuxFileCredentialBackend:
    def __init__(
        self,
        path: Path = DEFAULT_CREDENTIAL_PATH,
        *,
        require_root_owner: bool = True,
    ) -> None:
        if not path.is_absolute():
            raise ValueError("credential path must be absolute")
        self._path = path
        self._require_root_owner = require_root_owner

    def _validate_metadata(self) -> None:
        metadata = self._path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (self._require_root_owner and (metadata.st_uid != 0 or metadata.st_gid != 0))
        ):
            raise CredentialBackendError

    def read(self) -> bytes | None:
        if not self._path.exists():
            return None
        try:
            self._validate_metadata()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags)
            try:
                payload = os.read(descriptor, _MAXIMUM_CREDENTIAL_BYTES + 1)
            finally:
                os.close(descriptor)
            if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
                raise CredentialBackendError
            return payload
        except CredentialBackendError:
            raise
        except OSError:
            raise CredentialBackendError from None

    def write(self, payload: bytes) -> None:
        if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
            raise CredentialBackendError
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".credentials-",
                dir=self._path.parent,
            )
            temporary = Path(raw_path)
            try:
                os.fchmod(descriptor, 0o600)
                if self._require_root_owner:
                    os.fchown(descriptor, 0, 0)
                with os.fdopen(descriptor, "wb", closefd=True) as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.replace(temporary, self._path)
            temporary = None
            self._validate_metadata()
            directory = os.open(self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except CredentialBackendError:
            raise
        except OSError:
            raise CredentialBackendError from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def delete(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            raise CredentialBackendError from None


__all__ = ["DEFAULT_CREDENTIAL_PATH", "LinuxFileCredentialBackend"]
