#!/usr/bin/env python3
"""Local report over the proxy trace log (no Grafana needed).

Usage:
  ./scripts/report.py                 # summary of all traces
  ./scripts/report.py --last 200      # only the last N requests
  ./scripts/report.py --json          # machine readable output
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = int(len(values) * p)
    return values[min(k, len(values) - 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=os.environ.get(
        "SPLASH_OBS_TRACE_LOG",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "requests.jsonl")))
    ap.add_argument("--last", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        print("no traces yet at %s" % args.log)
        return

    records = []
    with open(args.log, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if args.last:
        records = records[-args.last:]

    durations = [r["duration_ms"] for r in records if r.get("duration_ms") is not None]
    ttfts = [r["ttft_ms"] for r in records if r.get("ttft_ms") is not None]
    tokens = defaultdict(int)
    statuses = Counter()
    finish = Counter()
    cache = Counter()
    models = Counter()
    streams = Counter()
    tool_calls = 0
    aborted = 0
    per_model_latency = defaultdict(list)

    for r in records:
        statuses[r.get("status_class", "unknown")] += 1
        models[r.get("model", "unknown")] += 1
        streams["stream" if r.get("stream") else "non-stream"] += 1
        finish[r.get("finish_reason") or "none"] += 1
        tool_calls += r.get("tool_calls", 0)
        if r.get("error_type") == "client_disconnect":
            aborted += 1
        usage = r.get("usage") or {}
        tokens["prompt"] += usage.get("prompt_tokens", 0)
        tokens["completion"] += usage.get("completion_tokens", 0)
        upstream = r.get("upstream") or {}
        cache[upstream.get("cache_status") or "unknown"] += 1
        if r.get("duration_ms") is not None:
            per_model_latency[r.get("model", "unknown")].append(r["duration_ms"])

    summary = {
        "requests": len(records),
        "duration_ms": {"p50": percentile(durations, 0.50),
                        "p95": percentile(durations, 0.95),
                        "max": max(durations) if durations else None},
        "ttft_ms": {"p50": percentile(ttfts, 0.50), "p95": percentile(ttfts, 0.95)},
        "tokens": dict(tokens),
        "tool_calls": tool_calls,
        "client_aborts": aborted,
        "statuses": dict(statuses),
        "finish_reasons": dict(finish),
        "cache_status": dict(cache),
        "models": dict(models),
        "stream_mix": dict(streams),
        "throughput_tokens_per_second": (
            round(tokens["completion"] / (sum(durations) / 1000.0), 2)
            if sum(durations) else None),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    print("requests          %d" % summary["requests"])
    print("duration p50/p95  %s / %s ms" % (summary["duration_ms"]["p50"], summary["duration_ms"]["p95"]))
    print("TTFT p50/p95      %s / %s ms" % (summary["ttft_ms"]["p50"], summary["ttft_ms"]["p95"]))
    print("tokens            prompt=%d completion=%d" % (tokens["prompt"], tokens["completion"]))
    print("effective tok/s   %s" % summary["throughput_tokens_per_second"])
    print("tool calls        %d   aborts=%d" % (tool_calls, aborted))
    print("statuses          %s" % ", ".join("%s=%d" % kv for kv in statuses.items()))
    print("finish reasons    %s" % ", ".join("%s=%d" % kv for kv in finish.items()))
    print("cache status      %s" % ", ".join("%s=%d" % kv for kv in cache.items()))
    print("models            %s" % ", ".join("%s=%d" % kv for kv in models.items()))
    for model, vals in per_model_latency.items():
        print("  %-50s p50=%s p95=%s ms" % (model, percentile(vals, 0.5), percentile(vals, 0.95)))


if __name__ == "__main__":
    main()
