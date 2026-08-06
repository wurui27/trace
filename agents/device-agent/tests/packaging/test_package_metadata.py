from __future__ import annotations

import plistlib
import re
from pathlib import Path

DEVICE_AGENT = Path(__file__).resolve().parents[2]
REPOSITORY = Path(__file__).resolve().parents[4]
PACKAGING = DEVICE_AGENT / "packaging"


def _text(relative: str) -> str:
    return (PACKAGING / relative).read_text(encoding="utf-8")


def test_every_native_service_runs_the_same_agent_entrypoint() -> None:
    with (PACKAGING / "macos/com.perfpilot.agent.plist").open("rb") as source:
        macos = plistlib.load(source)
    linux = _text("linux/perfpilot-agent.service")
    windows = _text("windows/service.py")

    assert macos["ProgramArguments"] == [
        "/Library/PerfPilot Agent/perfpilot-agent",
        "run",
    ]
    assert "ExecStart=/opt/perfpilot-agent/perfpilot-agent run" in linux
    assert 'AGENT_COMMAND = ("perfpilot-agent.exe", "run")' in windows


def test_packages_install_bootstrap_config_ca_adb_and_no_credentials() -> None:
    manifests = {
        "macos": _text("macos/build.sh") + _text("macos/scripts/postinstall"),
        "windows": _text("windows/build.ps1") + _text("windows/PerfPilotAgent.wxs"),
        "linux": _text("linux/build.sh") + _text("linux/postinst"),
    }

    for manifest in manifests.values():
        lowered = manifest.lower()
        assert "config.json" in lowered
        assert "perfpilot-ca.crt" in lowered
        assert "platform-tools" in lowered
        assert not re.search(r"source\s*=.*credentials", lowered)
        assert "registration_code" not in lowered
        assert "access_token" not in lowered
        assert "refresh_token" not in lowered


def test_frozen_build_includes_runtime_resources_and_excludes_tests() -> None:
    specification = _text("common/perfpilot-agent.spec")

    assert "resources/perfetto/startup.pbtxt" in specification
    assert "resources/perfetto/scroll.pbtxt" in specification
    assert "contracts/v1/agents" in specification
    assert "perfpilot_agent.__main__" in specification
    assert "tests" in specification
    assert "credential" not in specification.lower()


def test_native_services_have_required_security_and_lifecycle_settings() -> None:
    with (PACKAGING / "macos/com.perfpilot.agent.plist").open("rb") as source:
        macos = plistlib.load(source)
    linux = _text("linux/perfpilot-agent.service")
    windows = _text("windows/PerfPilotAgent.wxs")

    assert macos["Label"] == "com.perfpilot.agent"
    assert macos["RunAtLoad"] is True
    assert macos["KeepAlive"]["SuccessfulExit"] is False
    assert "NoNewPrivileges=true" in linux
    assert "PrivateTmp=true" in linux
    assert "PrivateDevices=false" in linux
    assert "ReadWritePaths=/var/lib/perfpilot-agent" in linux
    assert 'Account="LocalSystem"' in windows
    assert 'Start="auto"' in windows
    assert 'DelayedAutoStart="yes"' in windows


def test_install_scripts_are_idempotent_and_never_delete_credentials_on_upgrade() -> None:
    mac_preinstall = _text("macos/scripts/preinstall")
    mac_postinstall = _text("macos/scripts/postinstall")
    mac_uninstall = _text("macos/uninstall.sh")
    linux_builder = _text("linux/build.sh")
    linux_postinstall = _text("linux/postinst")
    linux_prerm = _text("linux/prerm")

    assert "bootout system/com.perfpilot.agent" in mac_preinstall
    assert "bootstrap system" in mac_postinstall
    assert "add-trusted-cert" not in mac_postinstall
    assert "remove-trusted-cert" not in mac_uninstall
    assert '[ -L "$ADB_DIR/adb" ]' not in linux_builder
    assert "systemctl daemon-reload" in linux_postinstall
    assert "systemctl enable" in linux_postinstall
    assert 'if [ "${1:-}" = "remove" ]' in linux_prerm
    combined = mac_preinstall + mac_postinstall + linux_postinstall + linux_prerm
    assert "credentials.json" not in combined
    assert "credentials.dat" not in combined


def test_package_workflow_builds_natively_and_publishes_checksums() -> None:
    workflow = (REPOSITORY / ".github/workflows/device-agent-packages.yml").read_text(
        encoding="utf-8"
    )

    for runner in ("macos-15", "windows-2025", "ubuntu-24.04"):
        assert runner in workflow
    for suffix in (".pkg", ".msi", ".deb"):
        assert suffix in workflow
    assert "SHA256SUMS" in workflow
    assert "doctor --json" in workflow
    assert "upgrade-smoke" in workflow
    assert "uninstall-smoke" in workflow


def test_ci_gate_runs_agent_tests_and_unsigned_warning_is_documented() -> None:
    ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    readme = _text("README.md")

    assert "agents/device-agent/src agents/device-agent/tests" in ci
    assert "agents/device-agent/tests" in ci
    assert "unsigned" in readme.lower()
    assert "developer id" in readme.lower()
    assert "windows code signing" in readme.lower()
