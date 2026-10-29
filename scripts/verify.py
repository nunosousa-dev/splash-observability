#!/usr/bin/env python3
"""Assert stack health and data flow, allowing for scrape/provisioning delays.

No inference is generated unless --traces is supplied. No synthetic alerts are
injected. --notify additionally requires the host desktop listener to be running.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from smoke import proxy_base


class Verifier:
    def __init__(self):
        self.proxy = proxy_base(os.environ.get("SPLASH_OBS_PROXY", "http://127.0.0.1:8100"))
        self.metrics = os.environ.get("SPLASH_OBS_METRICS", "http://127.0.0.1:8101").rstrip("/")
        self.prom = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
        self.am = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093").rstrip("/")
        self.grafana = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
        self.loki = os.environ.get("LOKI_URL", "http://localhost:3100").rstrip("/")
        self.notify = "http://%s:%s" % (os.environ.get("SPLASH_NOTIFY_HOST", "127.0.0.1"),
                                         os.environ.get("SPLASH_NOTIFY_PORT", "9393"))

    def get(self, url, params=None, text=False):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=5) as response:
            raw = response.read().decode("utf-8")
        return raw if text else json.loads(raw)

    def prometheus_data(self, path, params=None):
        response = self.get(self.prom + "/api/v1/" + path, params)
        if response.get("status") != "success":
            raise ValueError("Prometheus API did not return success")
        return response["data"]

    def targets(self):
        targets = self.prometheus_data("targets")["activeTargets"]
        expected = {"splash-proxy", "splash-runtime", "prometheus-self", "loki"}
        healthy = {t["labels"].get("job") for t in targets if t["health"] == "up"}
        missing = expected - healthy
        if missing:
            raise ValueError("missing/down: " + ", ".join(sorted(missing)))
        return "4/4 expected jobs up"

    def rules(self):
        groups = self.prometheus_data("rules")["groups"]
        rules = [r for g in groups if g["name"] == "splash-inference" for r in g["rules"]]
        if len(rules) != 12:
            raise ValueError("expected 12 Splash rules, found %d" % len(rules))
        bad = [r["name"] for r in rules if r.get("health") != "ok"
               or r.get("lastEvaluation", "0001").startswith("0001")]
        if bad:
            raise ValueError("unevaluated/unhealthy: " + ", ".join(bad))
        return "12 rules evaluated successfully"

    def alertmanager(self):
        active = self.prometheus_data("alertmanagers")["activeAlertmanagers"]
        if not any("alertmanager:9093" in a["url"] for a in active):
            raise ValueError("Prometheus has no alertmanager:9093 destination")
        status = self.get(self.am + "/api/v2/status")
        receivers = self.get(self.am + "/api/v2/receivers")
        # Alertmanager deliberately redacts webhook URLs as <secret> in /status.
        if "desktop" not in {r["name"] for r in receivers} or "webhook_configs:" not in status["config"]["original"]:
            raise ValueError("desktop webhook receiver is not loaded")
        return "Prometheus destination and desktop receiver loaded"

    def dashboards(self):
        for uid, expected_type in (("prometheus", "prometheus"), ("loki", "loki")):
            source = self.get(self.grafana + "/api/datasources/uid/" + uid)
            if source.get("uid") != uid or source.get("type") != expected_type:
                raise ValueError("missing/incorrect datasource UID: " + uid)
        items = self.get(self.grafana + "/api/search", {"query": "splash", "type": "dash-db"})
        expected = {"Splash request traces", "Splash runtime - inference engine"}
        found = {item["title"] for item in items if item.get("folderTitle") == "Splash"}
        if expected - found:
            raise ValueError("missing: " + ", ".join(sorted(expected - found)))
        for item in items:
            if item["title"] == "Splash request traces":
                dashboard = self.get(self.grafana + "/api/dashboards/uid/" + item["uid"])["dashboard"]
                if not any(p.get("id") == 13 for p in dashboard["panels"]):
                    raise ValueError("stream chunk-gap panel is not provisioned yet")
        return "datasource UIDs, both dashboards and chunk-gap panel provisioned"

    def query(self, expression, minimum=0):
        result = self.prometheus_data("query", {"query": expression})["result"]
        if not result or not all(float(r["value"][1]) >= minimum for r in result):
            raise ValueError("missing/invalid samples for " + expression)
        return "samples present"

    def traces(self, start_ns=None):
        response = self.get(self.loki + "/loki/api/v1/query_range", {
            "query": '{app="splash-obs"}', "limit": 20,
            "start": start_ns or time.time_ns() - 600 * 10**9, "end": time.time_ns(),
        })
        if response.get("status") != "success":
            raise ValueError("Loki query failed")
        entries = sum(len(stream["values"]) for stream in response["data"]["result"])
        if start_ns is not None and entries < 4:
            raise ValueError("expected 4 new smoke traces, found %d" % entries)
        return "%d recent traces%s" % (entries, "" if entries else " (no traffic is OK without --traces)")

    def checks(self, start_ns=None, expected_requests=0, notify=False):
        checks = [
            ("Client listener", lambda: self.get(self.proxy + "/healthz")),
            ("Metrics listener", lambda: self.get(self.metrics + "/healthz")),
            ("Proxy metrics", lambda: self.get(self.metrics + "/metrics", text=True)),
            ("Splash passthrough", lambda: self.get(self.metrics + "/metrics/splash", text=True)),
            ("Prometheus targets", self.targets),
            ("Splash metrics stored", lambda: self.query("splash_info", 1)),
            ("Alert rules", self.rules),
            ("Alert delivery wiring", self.alertmanager),
            ("Grafana", self.dashboards),
            ("Loki ready", lambda: self.get(self.loki + "/ready", text=True)),
            ("Loki traces", lambda: self.traces(start_ns)),
        ]
        if start_ns is not None:
            checks += [
                ("Requests scraped", lambda: self.query("sum(splash_obs_requests_total)", expected_requests)),
                ("Streaming TTFT scraped", lambda: self.query("sum(splash_obs_ttft_seconds_count)", 1)),
                ("Stream chunk gap scraped", lambda: self.query("sum(splash_obs_itl_seconds_count)", 1)),
            ]
        if notify:
            checks.append(("Desktop listener", lambda: self.get(self.notify + "/healthz")))
        return checks


def run_checks(checks, wait):
    pending = dict(checks)
    errors = {}
    deadline = time.monotonic() + wait
    while pending:
        for name, check in list(pending.items()):
            try:
                detail = check()
            except Exception as exc:
                errors[name] = str(exc)
            else:
                print("  %-28s OK%s" % (name, ": " + detail if isinstance(detail, str)
                                             and len(detail) < 120 else ""), flush=True)
                del pending[name]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    for name in pending:
        print("  %-28s FAIL: %s" % (name, errors[name]))
    return not pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", action="store_true", help="send 4 live inference requests first")
    parser.add_argument("--notify", action="store_true", help="also require the desktop listener")
    parser.add_argument("--wait", type=float, default=60, help="readiness retry window in seconds")
    args = parser.parse_args()
    if args.wait < 0:
        parser.error("--wait must not be negative")
    verifier = Verifier()
    start_ns, expected = None, 0
    if args.traces:
        start_ns = time.time_ns()
        print("Smoke test (4 live inference requests)", flush=True)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("smoke.py")),
                                 "--n", "1", "--proxy", verifier.proxy])
        if result.returncode:
            print("Smoke test failed; stack verification cannot pass.")
            return 1
        # Compare Prometheus with the current source, not a stale pre-smoke scrape.
        metrics = verifier.get(verifier.metrics + "/metrics", text=True)
        expected = sum(float(line.rsplit(" ", 1)[1]) for line in metrics.splitlines()
                       if line.startswith("splash_obs_requests_total{"))
    success = run_checks(verifier.checks(start_ns, expected, args.notify), args.wait)
    print("\nAll checks passed." if success else "\nSome checks failed.")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
