#!/bin/bash
set -euo pipefail

if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "Run this uninstaller as root" >&2
  exit 2
fi

REMOVE_DATA="false"
case "${1:-}" in
  "") ;;
  --remove-data) REMOVE_DATA="true" ;;
  --keep-data) ;;
  *) echo "Use --keep-data or --remove-data" >&2; exit 2 ;;
esac

STATE_DIR="/Library/Application Support/PerfPilot Agent"
CA_FILE="$STATE_DIR/perfpilot-ca.crt"
/bin/launchctl bootout system/com.perfpilot.agent >/dev/null 2>&1 || true
if [ -f "$CA_FILE" ]; then
  /usr/bin/security remove-trusted-cert -d "$CA_FILE" >/dev/null 2>&1 || true
fi
/bin/rm -f "/Library/LaunchDaemons/com.perfpilot.agent.plist"
/bin/rm -rf "/Library/PerfPilot Agent" "/Library/Logs/PerfPilot Agent"
if [ "$REMOVE_DATA" = "true" ]; then
  /usr/bin/security delete-generic-password \
    -s com.perfpilot.agent \
    -a credentials \
    /Library/Keychains/System.keychain >/dev/null 2>&1 || true
  /bin/rm -rf "$STATE_DIR"
fi
/usr/sbin/pkgutil --forget com.perfpilot.agent >/dev/null 2>&1 || true
