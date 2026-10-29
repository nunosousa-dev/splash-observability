#!/usr/bin/env python3
"""Smoke test: sends a handful of requests through the proxy and prints what was recorded.

Usage:
  ./scripts/smoke.py [--proxy http://127.0.0.1:8100] [--model <model>]
"""

import argparse
import json
import os
import time
import urllib.request


def chat(base, model, messages, stream=False, tools=None, max_tokens=60):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    resp = urllib.request.urlopen(req, timeout=180)
    body = resp.read()
    elapsed = time.monotonic() - start
    try:
        obj = json.loads(body)
    except Exception:
        obj = {}
    usage = obj.get("usage", {})
    finish = (obj.get("choices", [{}])[0].get("finish_reason")
              if obj.get("choices") else None)
    return {"status": resp.status, "elapsed_ms": round(elapsed * 1000),
            "usage": usage, "finish_reason": finish}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=os.environ.get("SPLASH_OBS_PROXY", "http://127.0.0.1:8100"))
    ap.add_argument("--model", default=os.environ.get(
        "SPLASH_MODEL", "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0"))
    ap.add_argument("--n", type=int, default=4)
    args = ap.parse_args()

    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]},
        },
    }]

    print("models:", urllib.request.urlopen(args.proxy + "/v1/models", timeout=30).read()[:120])

    cases = [
        ("plain", [{"role": "user", "content": "Explain KV cache in one sentence."}], False, None),
        ("stream", [{"role": "user", "content": "Count from 1 to 10."}], True, None),
        ("tool", [{"role": "user", "content": "What is the weather in Lisbon?"}], False, tools),
        ("long context", [{"role": "user", "content": "Summarise this: " + "The quick brown fox. " * 200}], False, None),
    ]

    for i in range(args.n):
        for name, messages, stream, tool_list in cases:
            try:
                result = chat(args.proxy, args.model, messages, stream, tool_list)
                print("%-12s %s" % (name, result))
            except Exception as exc:
                print("%-12s FAILED: %s" % (name, exc))

    print("\nproxy metrics sample:")
    metrics = urllib.request.urlopen(args.proxy + "/metrics", timeout=30).read().decode()
    for line in metrics.splitlines():
        if line.startswith(("splash_obs_requests_total", "splash_obs_tokens_total",
                            "splash_obs_tool_calls_total", "splash_obs_ttft_seconds_count")):
            print("  " + line)

    print("\nupstream health:")
    upstream = urllib.request.urlopen(args.proxy + "/metrics/splash", timeout=30).read().decode()
    for line in upstream.splitlines():
        if line.startswith(("splash_decode_tokens_per_second", "splash_ttft_p95_milliseconds",
                            "splash_requests_failed_total", "splash_draft_acceptance_ratio")):
            print("  " + line)


if __name__ == "__main__":
    main()
