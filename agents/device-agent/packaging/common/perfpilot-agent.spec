# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


SPEC_PATH = Path(SPEC).resolve()
DEVICE_AGENT_ROOT = SPEC_PATH.parents[2]
REPOSITORY_ROOT = SPEC_PATH.parents[4]
SOURCE_ROOT = DEVICE_AGENT_ROOT / "src"
AGENT_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "v1" / "agents"

# Runtime entrypoint: perfpilot_agent.__main__
ENTRYPOINT = SOURCE_ROOT / "perfpilot_agent" / "__main__.py"
DATAS = [
    (
        str(SOURCE_ROOT / "perfpilot_agent/resources/perfetto/startup.pbtxt"),
        "perfpilot_agent/resources/perfetto",
    ),
    (
        str(SOURCE_ROOT / "perfpilot_agent/resources/perfetto/scroll.pbtxt"),
        "perfpilot_agent/resources/perfetto",
    ),
    (str(AGENT_CONTRACT_ROOT), "contracts/v1/agents"),
]
HIDDEN_IMPORTS = [
    "perfpilot_agent.platform.linux",
    "perfpilot_agent.platform.macos",
    "perfpilot_agent.platform.windows",
]
if sys.platform == "win32":
    HIDDEN_IMPORTS.extend(("ntsecuritycon", "win32crypt", "win32security"))

analysis = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=1,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="perfpilot-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
