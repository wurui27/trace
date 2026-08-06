#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DEVICE_AGENT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
SPEC_PATH="$DEVICE_AGENT_ROOT/packaging/common/perfpilot-agent.spec"
VALIDATOR="$DEVICE_AGENT_ROOT/packaging/common/validate_bootstrap.py"

CONFIG_PATH=""
CA_PATH=""
ADB_DIR=""
OUTPUT_PATH=""
VERSION="0.1.0"
PYTHON_BIN="python3"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) CONFIG_PATH="${2:-}"; shift 2 ;;
    --ca) CA_PATH="${2:-}"; shift 2 ;;
    --adb-dir) ADB_DIR="${2:-}"; shift 2 ;;
    --output) OUTPUT_PATH="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --python) PYTHON_BIN="${2:-}"; shift 2 ;;
    *) echo "Unknown argument" >&2; exit 2 ;;
  esac
done

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
   [ -z "$CONFIG_PATH" ] || [ -z "$CA_PATH" ] || [ -z "$ADB_DIR" ] ||
   [ -z "$OUTPUT_PATH" ]; then
  echo "Missing or invalid package build arguments" >&2
  exit 2
fi
if [ ! -f "$CONFIG_PATH" ] || [ -L "$CONFIG_PATH" ] ||
   [ ! -f "$CA_PATH" ] || [ -L "$CA_PATH" ] ||
   [ ! -x "$ADB_DIR/adb" ] || [ -L "$ADB_DIR/adb" ]; then
  echo "Package inputs are invalid" >&2
  exit 2
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "Required Linux build tools are unavailable" >&2
  exit 2
fi

ARCHITECTURE="$(dpkg --print-architecture)"
case "$ARCHITECTURE" in
  amd64|arm64) ;;
  *) echo "Unsupported Linux architecture" >&2; exit 2 ;;
esac
OUTPUT_PARENT="$(dirname "$OUTPUT_PATH")"
mkdir -p "$OUTPUT_PARENT"
OUTPUT_PARENT="$(cd "$OUTPUT_PARENT" && pwd -P)"
OUTPUT_PATH="$OUTPUT_PARENT/$(basename "$OUTPUT_PATH")"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/perfpilot-agent-linux.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT

"$PYTHON_BIN" "$VALIDATOR" --platform linux --config "$CONFIG_PATH" --ca "$CA_PATH"
"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$BUILD_ROOT/dist" \
  --workpath "$BUILD_ROOT/work" \
  "$SPEC_PATH"

PACKAGE_ROOT="$BUILD_ROOT/package"
install -d -m 0755 \
  "$PACKAGE_ROOT/DEBIAN" \
  "$PACKAGE_ROOT/opt/perfpilot-agent/platform-tools" \
  "$PACKAGE_ROOT/etc/perfpilot-agent" \
  "$PACKAGE_ROOT/lib/systemd/system" \
  "$PACKAGE_ROOT/var/lib/perfpilot-agent"
install -m 0755 "$BUILD_ROOT/dist/perfpilot-agent" \
  "$PACKAGE_ROOT/opt/perfpilot-agent/perfpilot-agent"
install -m 0755 "$ADB_DIR/adb" "$PACKAGE_ROOT/opt/perfpilot-agent/platform-tools/adb"
install -m 0644 "$CONFIG_PATH" "$PACKAGE_ROOT/etc/perfpilot-agent/config.json"
install -m 0644 "$CA_PATH" "$PACKAGE_ROOT/etc/perfpilot-agent/perfpilot-ca.crt"
install -m 0644 "$SCRIPT_DIR/perfpilot-agent.service" \
  "$PACKAGE_ROOT/lib/systemd/system/perfpilot-agent.service"
install -m 0755 "$SCRIPT_DIR/postinst" "$PACKAGE_ROOT/DEBIAN/postinst"
install -m 0755 "$SCRIPT_DIR/prerm" "$PACKAGE_ROOT/DEBIAN/prerm"
install -m 0755 "$SCRIPT_DIR/postrm" "$PACKAGE_ROOT/DEBIAN/postrm"
install -m 0644 "$SCRIPT_DIR/control" "$PACKAGE_ROOT/DEBIAN/control"
sed -i \
  -e "s/@VERSION@/$VERSION/g" \
  -e "s/@ARCHITECTURE@/$ARCHITECTURE/g" \
  "$PACKAGE_ROOT/DEBIAN/control"

dpkg-deb --root-owner-group --build "$PACKAGE_ROOT" "$OUTPUT_PATH"
