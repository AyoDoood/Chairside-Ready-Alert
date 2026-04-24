#!/bin/bash
# This file must stay in the same folder as chairside_ready_alert.py.
# If double-clicking fails (common with GitHub ZIP: missing execute bit), use Terminal:
#   cd to this folder, then:  bash install_chairside_ready_alert_macos.sh
set -euo pipefail
cd "$(dirname "$0")"
exec bash "install_chairside_ready_alert_macos.sh"
