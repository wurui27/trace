from __future__ import annotations

import os
import tempfile
from pathlib import Path

from perfpilot_agent.credentials import CredentialBackendError

_MAXIMUM_CREDENTIAL_BYTES = 64 * 1024


def restrict_file_to_current_user(path: Path) -> None:
    """Install a protected DACL containing only the process user."""

    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_QUERY,
        )
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_ALL_ACCESS,
            user_sid,
        )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        descriptor.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED,
            win32security.SE_DACL_PROTECTED,
        )
        win32security.SetFileSecurity(
            str(path),
            win32security.DACL_SECURITY_INFORMATION,
            descriptor,
        )
    except Exception:
        raise OSError("unable to protect private Agent file") from None


def default_credential_path() -> Path:
    program_data = os.environ.get("ProgramData")
    if not program_data:
        raise CredentialBackendError
    return Path(program_data) / "PerfPilot" / "Agent" / "credentials.dat"


class WindowsDpapiCredentialBackend:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_credential_path()

    @staticmethod
    def _protect(payload: bytes) -> bytes:
        try:
            import win32crypt

            return win32crypt.CryptProtectData(
                payload,
                "PerfPilot Agent",
                None,
                None,
                None,
                win32crypt.CRYPTPROTECT_LOCAL_MACHINE,
            )
        except (ImportError, OSError, ValueError):
            raise CredentialBackendError from None

    @staticmethod
    def _unprotect(payload: bytes) -> bytes:
        try:
            import win32crypt

            _description, cleartext = win32crypt.CryptUnprotectData(
                payload,
                None,
                None,
                None,
                0,
            )
            return cleartext
        except (ImportError, OSError, ValueError):
            raise CredentialBackendError from None

    @staticmethod
    def _restrict_to_system(path: Path) -> None:
        try:
            import ntsecuritycon
            import win32security

            system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
            acl = win32security.ACL()
            acl.AddAccessAllowedAce(
                win32security.ACL_REVISION,
                ntsecuritycon.FILE_ALL_ACCESS,
                system_sid,
            )
            descriptor = win32security.SECURITY_DESCRIPTOR()
            descriptor.SetSecurityDescriptorDacl(True, acl, False)
            win32security.SetFileSecurity(
                str(path),
                win32security.DACL_SECURITY_INFORMATION,
                descriptor,
            )
        except (ImportError, OSError):
            raise CredentialBackendError from None

    def read(self) -> bytes | None:
        if not self._path.exists():
            return None
        try:
            encrypted = self._path.read_bytes()
        except OSError:
            raise CredentialBackendError from None
        if not encrypted or len(encrypted) > _MAXIMUM_CREDENTIAL_BYTES * 2:
            raise CredentialBackendError
        payload = self._unprotect(encrypted)
        if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
            raise CredentialBackendError
        return payload

    def write(self, payload: bytes) -> None:
        if not payload or len(payload) > _MAXIMUM_CREDENTIAL_BYTES:
            raise CredentialBackendError
        encrypted = self._protect(payload)
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(prefix=".credentials-", dir=self._path.parent)
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb", closefd=True) as target:
                target.write(encrypted)
                target.flush()
                os.fsync(target.fileno())
            self._restrict_to_system(temporary)
            os.replace(temporary, self._path)
            temporary = None
            self._restrict_to_system(self._path)
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


__all__ = [
    "WindowsDpapiCredentialBackend",
    "default_credential_path",
    "restrict_file_to_current_user",
]
