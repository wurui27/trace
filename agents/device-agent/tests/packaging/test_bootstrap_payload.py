from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


DEVICE_AGENT = Path(__file__).resolve().parents[2]
COMMON = DEVICE_AGENT / "packaging/common"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("platform_name", ["macos", "windows", "linux"])
def test_ci_fixture_is_a_valid_secret_free_native_bootstrap(
    tmp_path: Path, platform_name: str
) -> None:
    output = tmp_path / platform_name

    created = _run(
        str(COMMON / "create_ci_fixture.py"),
        "--platform",
        platform_name,
        "--output",
        str(output),
    )
    validated = _run(
        str(COMMON / "validate_bootstrap.py"),
        "--platform",
        platform_name,
        "--config",
        str(output / "config.json"),
        "--ca",
        str(output / "perfpilot-ca.crt"),
    )

    assert created.returncode == 0, created.stderr
    assert validated.returncode == 0, validated.stderr
    assert sorted(path.name for path in output.iterdir()) == [
        "config.json",
        "perfpilot-ca.crt",
    ]
    config = json.loads((output / "config.json").read_text(encoding="utf-8"))
    assert not any(
        marker in key.lower()
        for key in config
        for marker in ("credential", "password", "private", "secret", "token")
    )


def test_validator_rejects_secret_fields(tmp_path: Path) -> None:
    output = tmp_path / "linux"
    assert (
        _run(
            str(COMMON / "create_ci_fixture.py"),
            "--platform",
            "linux",
            "--output",
            str(output),
        ).returncode
        == 0
    )
    config_path = output / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["access_token"] = "must-not-ship"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = _run(
        str(COMMON / "validate_bootstrap.py"),
        "--platform",
        "linux",
        "--config",
        str(config_path),
        "--ca",
        str(output / "perfpilot-ca.crt"),
    )

    assert result.returncode != 0


def test_validator_rejects_paths_for_another_platform(tmp_path: Path) -> None:
    output = tmp_path / "linux"
    assert (
        _run(
            str(COMMON / "create_ci_fixture.py"),
            "--platform",
            "linux",
            "--output",
            str(output),
        ).returncode
        == 0
    )

    result = _run(
        str(COMMON / "validate_bootstrap.py"),
        "--platform",
        "macos",
        "--config",
        str(output / "config.json"),
        "--ca",
        str(output / "perfpilot-ca.crt"),
    )

    assert result.returncode != 0
