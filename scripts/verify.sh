#!/usr/bin/env bash
# End-to-end check of the observability stack. Run after ./scripts/up.sh.
#
#   ./scripts/verify.sh            # health, scrape targets, dashboards, alerts, traces
#   ./scripts/verify.sh --traces   # send a few requests through the proxy first
#
# Exits non-zero if any check fails.
set -euo pipefail

PROXY="${SPLASH_OBS_PROXY:-http://127.0.0.1:8100}"
METRICS="${SPLASH_OBS_METRICS:-http://127.0.0.1:8101}"
PROM="${PROMETHEUS_URL:-http://localhost:9090}"
AM="${ALERTMANAGER_URL:-http://localhost:9093}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"
LOKI="${LOKI_URL:-http://localhost:3100}"

fail=0

check() {                       # check <name> <command...>
  name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf "  %-32s ok\n" "$name"
  else
    printf "  %-32s FAIL\n" "$name"
    fail=1
  fi
}

query() {                       # query <prometheus expr>
  curl -fsG "$PROM/api/v1/query" --data-urlencode "query=$1" | python3 -c '
import sys, json
result = json.load(sys.stdin)["data"]["result"]
if not result:
    print("  none")
for r in result:
    labels = ", ".join("%s=%s" % kv for kv in sorted(r["metric"].items()))
    value = r.get("value")
    if isinstance(value, list):          # some Prometheus versions return [ts, value]
        value = value[-1]
    print("  %s = %s" % (labels or "(no labels)", value))
'
}

if [ "${1:-}" = "--traces" ]; then
  echo "Smoke test (talks to Splash, may take a minute)"
  python3 "$(dirname "${BASH_SOURCE[0]}")/smoke.py" --n 1
  echo
fi

echo "Proxy"
check "client port /healthz"        curl -fs "$PROXY/healthz"
check "metrics port /healthz"       curl -fs "$METRICS/healthz"
check "metrics port /metrics"       curl -fs "$METRICS/metrics"
check "upstream passthrough"        curl -fs "$METRICS/metrics/splash"

echo "Prometheus"
check "prometheus healthy"          curl -fs "$PROM/-/healthy"
printf "  scrape targets:\n"
curl -fs "$PROM/api/v1/targets" | python3 -c 'import sys,json; [print("    %-16s %-6s %s" % (t["labels"].get("job"), t["health"], t.get("lastError") or t.get("lastScrape"))) for t in json.load(sys.stdin)["data"]["activeTargets"]]'
printf "  proxy metrics seen by Prometheus:\n"; query "sum(splash_obs_requests_total)"
printf "  splash runtime metrics seen by Prometheus:\n"; query "splash_decode_tokens_per_second"
printf "  recording rules loaded:\n"; query "splash:ttft_p95"

echo "Alerting"
printf "  rules evaluated:\n"
curl -fs "$PROM/api/v1/rules" | python3 -c 'import sys,json; [print("    %-24s %s rules, health %s" % (g["name"], len(g["rules"]), g.get("health"))) for g in json.load(sys.stdin)["data"]["groups"]]'
check "alertmanager healthy"        curl -fs "$AM/api/v2/alerts"

echo "Grafana"
check "grafana healthy"             curl -fs "$GRAFANA/api/health"
printf "  dashboards in the Splash folder:\n"
curl -fs "$GRAFANA/api/search?query=splash" | python3 -c 'import sys,json; items=[x for x in json.load(sys.stdin) if x["type"]=="dash-db"]; print("    none" if not items else "\n".join("    %s" % x["title"] for x in items))'

echo "Loki"
check "loki ready"                  curl -fs "$LOKI/ready"
printf "  traces in loki (last 10m):\n"
now_ns=$(python3 -c 'import time; print(int(time.time() * 1e9))')
start_ns=$(python3 -c 'import time; print(int((time.time() - 600) * 1e9))')
curl -fsG "$LOKI/loki/api/v1/query_range" \
  --data-urlencode 'query={app="splash-obs"}' \
  --data-urlencode "start=$start_ns" \
  --data-urlencode "end=$now_ns" \
  --data-urlencode "limit=5" \
  | python3 -c 'import sys,json; r=json.load(sys.stdin).get("data",{}).get("result",[]); total=sum(len(s.get("values") or s.get("entries") or []) for s in r); print("    none" if not r else "    %d entries across %d stream(s)" % (total, len(r)))'

if [ "$fail" -ne 0 ]; then
  echo
  echo "Some checks failed."
  exit 1
fi
echo
echo "All checks passed."
