#!/usr/bin/env bash

set -euo pipefail

fail() {
  printf '%s\n' '拒绝清理：分析数据目录未通过安全校验。' >&2
  exit 1
}

if [[ "${PERFPILOT_RESET_ANALYSIS_DATA:-}" == "false" ]]; then
  exit 0
fi
[[ "${PERFPILOT_RESET_ANALYSIS_DATA:-}" == "true" ]] || fail
[[ -n "${PERFPILOT_LOCAL_DATA_DIR:-}" ]] || fail
[[ -n "${PERFPILOT_EXPECTED_ANALYSIS_ROOT:-}" ]] || fail
[[ "$PERFPILOT_LOCAL_DATA_DIR" == "$PERFPILOT_EXPECTED_ANALYSIS_ROOT" ]] || fail
[[ -n "${PERFPILOT_LOCAL_STATE_DIR:-}" ]] || fail

python3 - "$PERFPILOT_LOCAL_DATA_DIR" "$PERFPILOT_LOCAL_STATE_DIR" <<'PY' || exit 1
import os
import stat
import sys
from pathlib import Path

data_text, state_text = sys.argv[1:]
data = Path(data_text)
state = Path(state_text)
if not data.is_absolute() or not state.is_absolute():
    raise SystemExit(1)
if os.path.normpath(data_text) != data_text or os.path.normpath(state_text) != state_text:
    raise SystemExit(1)
if data == Path("/") or data == Path.home() or data.name in {"home", "state", "config", "project"}:
    raise SystemExit(1)
if data == state or data in state.parents or state in data.parents:
    raise SystemExit(1)

current = Path("/")
for component in data.parts[1:]:
    current /= component
    try:
        status = os.lstat(current)
    except OSError:
        raise SystemExit(1) from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise SystemExit(1)
PY

rm -rf -- "$PERFPILOT_LOCAL_DATA_DIR"
install -d -m 0700 -- "$PERFPILOT_LOCAL_DATA_DIR"
