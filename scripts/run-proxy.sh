#!/usr/bin/env bash
# Run the observability proxy in the foreground (Ctrl-C to stop).
#
#   SPLASH_UPSTREAM_PORT=8000 SPLASH_OBS_PORT=8100 ./scripts/run-proxy.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then PYTHON=/usr/bin/python3; fi
exec "$PYTHON" "$HERE/proxy/splash_obs_proxy.py"
