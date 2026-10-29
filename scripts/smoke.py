#!/usr/bin/env python3
"""Smoke test: sends a handful of requests through the proxy and prints what was recorded.

Usage:
  ./scripts/smoke.py [--proxy http://127.0.0.1:8100] [--model <model>]
"""

import argparse
import json
import os
import sys
import time
import urllib.request


def proxy_base(url):
    """Accept either the server URL or an OpenAI-style base URL ending in /v1."""
    base = url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def response_summary(body, content_type):
    """Fold plain JSON or complete SSE events, including the final usage frame."""
    if "text/event-stream" in content_type:
        objects = []
        data = []
        done = False
        for line in body.decode("utf-8").splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                payload = "\n".join(data)
                data = []
                if payload == "[DONE]":
                    done = True
                    break
                objects.append(json.loads(payload))
        if not done:
            raise ValueError("stream ended without data: [DONE]")
    else:
        objects = [json.loads(body)]

    usage, finish = {}, None
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError("expected a JSON object in the response")
        if obj.get("error"):
            raise ValueError("upstream returned an error event")
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for choice in obj.get("choices", []):
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    return {"usage": usage, "finish_reason": finish}


def chat(base, model, messages, stream=False, tools=None, max_tokens=60):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        proxy_base(base) + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as resp:
        summary = response_summary(resp.read(), resp.headers.get("Content-Type", ""))
        return {"status": resp.status,
                "elapsed_ms": round((time.monotonic() - start) * 1000), **summary}


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=os.environ.get("SPLASH_OBS_PROXY", "http://127.0.0.1:8100"))
    ap.add_argument("--model", default=os.environ.get(
        "SPLASH_MODEL", "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0"))
    ap.add_argument("--n", type=int, default=4)
    args = ap.parse_args()
    if args.n < 1:
        ap.error("--n must be at least 1")
    args.proxy = proxy_base(args.proxy)

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

    print("models:", fetch(args.proxy + "/v1/models")[:120])

    cases = [
        ("plain", [{"role": "user", "content": "Explain KV cache in one sentence."}], False, None),
        ("stream", [{"role": "user", "content": "Count from 1 to 10."}], True, None),
        ("tool", [{"role": "user", "content": "What is the weather in Lisbon?"}], False, tools),
        ("long context", [{"role": "user", "content": "Summarise this: " + "The quick brown fox. " * 200}], False, None),
    ]

    failures = 0
    for i in range(args.n):
        for name, messages, stream, tool_list in cases:
            try:
                result = chat(args.proxy, args.model, messages, stream, tool_list)
                print("%-12s %s" % (name, result))
            except Exception as exc:
                failures += 1
                print("%-12s FAILED: %s" % (name, exc))

    print("\nproxy metrics sample:")
    metrics = fetch(args.proxy + "/metrics").decode()
    for line in metrics.splitlines():
        if line.startswith(("splash_obs_requests_total", "splash_obs_tokens_total",
                            "splash_obs_tool_calls_total", "splash_obs_ttft_seconds_count",
                            "splash_obs_itl_seconds_count")):
            print("  " + line)

    print("\nupstream health:")
    upstream = fetch(args.proxy + "/metrics/splash").decode()
    for line in upstream.splitlines():
        if line.startswith(("splash_decode_tokens_per_second", "splash_ttft_p95_milliseconds",
                            "splash_requests_failed_total", "splash_draft_acceptance_ratio")):
            print("  " + line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
