#!/usr/bin/env bash
# Stop the stack and the proxy.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER="${DOCKER:-docker}"
if ! command -v "$DOCKER" >/dev/null 2>&1; then DOCKER=/opt/homebrew/bin/docker; fi

pkill -f splash_obs_proxy.py && echo "==> proxy stopped" || echo "==> proxy was not running"
"$DOCKER" compose -f "$HERE/docker-compose.yml" down
echo "==> stack stopped (volumes kept: prometheus-data, grafana-data, loki-data)"
