from __future__ import annotations

import subprocess

from perfpilot_agent.platform.macos import MacOSKeychainCredentialBackend


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if "find-generic-password" in argv:
            return subprocess.CompletedProcess(argv, 44, b"", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_macos_credentials_use_the_current_users_default_keychain() -> None:
    runner = RecordingRunner()
    backend = MacOSKeychainCredentialBackend(runner=runner, effective_user_id=501)

    assert backend.read() is None
    backend.write(b"credentials")
    backend.delete()

    assert len(runner.calls) == 3
    assert all("/Library/Keychains/System.keychain" not in call for call in runner.calls)


def test_root_agent_credentials_remain_in_the_system_keychain() -> None:
    runner = RecordingRunner()
    backend = MacOSKeychainCredentialBackend(runner=runner, effective_user_id=0)

    assert backend.read() is None
    backend.write(b"credentials")
    backend.delete()

    assert all("/Library/Keychains/System.keychain" in call for call in runner.calls)
