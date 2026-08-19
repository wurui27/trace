#!/usr/bin/env bash

set -euo pipefail

systemctl --user stop perfpilot.target perfpilot-gateway.service perfpilot-web.service \
  perfpilot-api.service perfpilot-smartperfetto.service
systemctl --user start perfpilot-reset-analysis-data.service
systemctl --user start perfpilot.target
