#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_ENV_FILE="${PERFPILOT_LOCAL_AI_ENV_FILE:-$PROJECT_DIR/.perfpilot/local-control/perfpilot-ai.env}"

if [[ -f "$AI_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$AI_ENV_FILE"
  set +a
fi

cd "$PROJECT_DIR"
exec env PYTHONPATH="$PROJECT_DIR/services/api/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$PROJECT_DIR/.venv/bin/python" -c \
  "from perfpilot_api.local_app import run; run()"
