#!/usr/bin/env bash
# Start the observability stack: trace proxy + Prometheus + Grafana + Loki + Alertmanager.
#
#   ./scripts/up.sh              start everything
#   ./scripts/up.sh --restart    rebuild the proxy image and restart the stack
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DOCKER="${DOCKER:-}"
if [ -z "$DOCKER" ]; then
  if command -v docker >/dev/null 2>&1; then
    DOCKER=docker
  elif [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
    DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
  elif [ -x /opt/homebrew/bin/docker ]; then
    DOCKER=/opt/homebrew/bin/docker
  else
    echo "docker not found. Install Docker Desktop or set DOCKER=/path/to/docker"
    exit 1
  fi
fi

"$DOCKER" compose -f "$HERE/docker-compose.yml" up -d --build

echo
echo "Proxy        http://127.0.0.1:8100/v1   <- point your client at this"
echo "Prometheus   http://localhost:9090"
echo "Grafana      http://localhost:3000  (dashboards in the 'Splash' folder)"
echo "Alertmanager http://localhost:9093"
echo "Loki         http://localhost:3100"
echo
echo "Scrape target health:"
curl -s http://localhost:9090/api/v1/targets | python3 -c 'import sys,json; [print("  %-16s %s" % (t["labels"].get("job"), t["health"])) for t in json.load(sys.stdin)["targets"]]'
