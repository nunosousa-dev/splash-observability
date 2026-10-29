#!/usr/bin/env python3
"""
Splash observability proxy.

A transparent OpenAI-compatible proxy that sits between clients (OpenCode, scripts)
and the local Splash inference server, and turns every request into metadata-only
observability data.

What it captures
  - request/response metadata: model, stream, max_tokens, tools, message count,
    input chars, reasoning effort, finish reason, tool-call count, token usage
  - latency: total duration, TTFT (time to first token), inter-token latency
  - errors: status, error type, truncated error message
  - it NEVER stores prompt text or completion text

Endpoints
  CLIENT port (default 127.0.0.1:8100)
    /v1/*            transparent passthrough to Splash
    /healthz         proxy health
    /metrics         proxy metrics (Prometheus text format)
    /metrics/splash  upstream Splash metrics passthrough

  METRICS port (default 0.0.0.0:8101)  -- read-only, for Prometheus scraping
    /metrics
    /metrics/splash
    /healthz
    (any POST is rejected with 405; any other GET with 404)

Env vars
  SPLASH_UPSTREAM_HOST   default 127.0.0.1
  SPLASH_UPSTREAM_PORT   default 8000
  SPLASH_OBS_HOST        default 127.0.0.1
  SPLASH_OBS_PORT        default 8100
  SPLASH_OBS_METRICS_HOST default 0.0.0.0
  SPLASH_OBS_METRICS_PORT default 8101
  LOKI_URL               optional, e.g. http://127.0.0.1:3100  -> pushes traces to Loki
  SPLASH_OBS_TRACE_LOG   default logs/requests.jsonl
  SPLASH_OBS_TRACE_MAX_BYTES default 67108864 (64 MiB) -> rotate at this size, 0 disables
  SPLASH_OBS_TRACE_KEEP  default 4 -> total trace files kept, live one included
  SPLASH_UPSTREAM_TIMEOUT default 600 (seconds)

Requires only the Python standard library (Python 3.9+).
"""

import http.client
import http.server
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

VERSION = "1.0.0"

UPSTREAM_HOST = os.environ.get("SPLASH_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("SPLASH_UPSTREAM_PORT", "8000"))
# Splash accepts requests only from allow-listed hosts. When the proxy runs inside a
# container the connect target is host.docker.internal, so we present the loopback
# Host header. Set SPLASH_UPSTREAM_HOST_HEADER="" to send the real host instead.
UPSTREAM_HOST_HEADER = os.environ.get("SPLASH_UPSTREAM_HOST_HEADER", "127.0.0.1")
CLIENT_HOST = os.environ.get("SPLASH_OBS_HOST", "127.0.0.1")
CLIENT_PORT = int(os.environ.get("SPLASH_OBS_PORT", "8100"))
METRICS_HOST = os.environ.get("SPLASH_OBS_METRICS_HOST", "0.0.0.0")
METRICS_PORT = int(os.environ.get("SPLASH_OBS_METRICS_PORT", "8101"))
LOKI_URL = os.environ.get("LOKI_URL", "").strip()
TRACE_LOG = os.environ.get("SPLASH_OBS_TRACE_LOG", "logs/requests.jsonl")
UPSTREAM_TIMEOUT = float(os.environ.get("SPLASH_UPSTREAM_TIMEOUT", "600"))

BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
           10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0)

# Buckets for character counts. The latency buckets above are meaningless here:
# a 4,000 character prompt would land in +Inf.
CHAR_BUCKETS = (10, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000)

# Trace log rotation. Loki keeps 30d; without this the JSONL grows forever.
TRACE_MAX_BYTES = int(os.environ.get("SPLASH_OBS_TRACE_MAX_BYTES", 64 * 1024 * 1024))
TRACE_KEEP = max(2, int(os.environ.get("SPLASH_OBS_TRACE_KEEP", 4)))

log = logging.getLogger("splash-obs")


# ---------------------------------------------------------------- metrics ----

# The proxy is a ThreadingHTTPServer, so every mutation below happens on a random
# worker thread. A single lock protects all three primitives: it is only held for
# dict lookups and float adds (never during I/O), and contention is irrelevant at
# local inference request rates.
METRICS_LOCK = threading.Lock()


def _render_labels(labels):
    return ",".join('%s="%s"' % (k, str(v)) for k, v in labels)


class Counter:
    def __init__(self, name, help):
        self.name = name
        self.help = help
        self.values = defaultdict(float)

    def inc(self, labels, amount=1.0):
        with METRICS_LOCK:
            self.values[tuple(sorted(labels.items()))] += amount

    def render(self):
        out = ["# HELP %s %s" % (self.name, self.help),
               "# TYPE %s counter" % self.name]
        with METRICS_LOCK:
            items = sorted(self.values.items())
        for labels, value in items:
            if labels:
                out.append("%s{%s} %g" % (self.name, _render_labels(labels), value))
            else:
                out.append("%s %g" % (self.name, value))
        return "\n".join(out)


class Gauge:
    def __init__(self, name, help):
        self.name = name
        self.help = help
        self.values = {}

    def set(self, labels, value):
        with METRICS_LOCK:
            self.values[tuple(sorted(labels.items()))] = value

    def get(self, labels=(), default=0.0):
        if isinstance(labels, dict):
            labels = tuple(sorted(labels.items()))
        with METRICS_LOCK:
            return self.values.get(labels, default)

    def add(self, labels, delta, floor=0.0):
        """Atomically offset a gauge, clamped to `floor`. Returns the new value."""
        key = tuple(sorted(labels.items()))
        with METRICS_LOCK:
            current = self.values.get(key, 0.0)
            value = max(floor, current + delta)
            self.values[key] = value
        return value

    def render(self):
        out = ["# HELP %s %s" % (self.name, self.help),
               "# TYPE %s gauge" % self.name]
        with METRICS_LOCK:
            items = sorted(self.values.items())
        for labels, value in items:
            if labels:
                out.append("%s{%s} %g" % (self.name, _render_labels(labels), value))
            else:
                out.append("%s %g" % (self.name, value))
        return "\n".join(out)


class Histogram:
    def __init__(self, name, help, buckets=BUCKETS):
        self.name = name
        self.help = help
        self.buckets = list(buckets)
        self.data = {}  # labels -> [bucket_counts..., count, sum]

    def observe(self, labels, value):
        key = tuple(sorted(labels.items()))
        with METRICS_LOCK:
            if key not in self.data:
                self.data[key] = [0] * (len(self.buckets) + 2)
            row = self.data[key]
            for i, limit in enumerate(self.buckets):
                if value <= limit:
                    row[i] += 1
            row[len(self.buckets)] += 1
            row[len(self.buckets) + 1] += value

    def render(self):
        out = ["# HELP %s %s" % (self.name, self.help),
               "# TYPE %s histogram" % self.name]
        with METRICS_LOCK:
            items = sorted(self.data.items())
        for labels, row in items:
            label_str = _render_labels(labels)
            for i, limit in enumerate(self.buckets):
                if label_str:
                    out.append('%s_bucket{%s,le="%s"} %d' % (self.name, label_str, limit, row[i]))
                else:
                    out.append('%s_bucket{le="%s"} %d' % (self.name, limit, row[i]))
            if label_str:
                out.append('%s_bucket{%s,le="+Inf"} %d' % (self.name, label_str, row[len(self.buckets)]))
                out.append('%s_count{%s} %d' % (self.name, label_str, row[len(self.buckets)]))
                out.append('%s_sum{%s} %g' % (self.name, label_str, row[len(self.buckets) + 1]))
            else:
                out.append('%s_bucket{le="+Inf"} %d' % (self.name, row[len(self.buckets)]))
                out.append("%s_count %d" % (self.name, row[len(self.buckets)]))
                out.append("%s_sum %g" % (self.name, row[len(self.buckets) + 1]))
        return "\n".join(out)


COUNTERS = {
    "splash_obs_requests_total": Counter(
        "splash_obs_requests_total", "Requests forwarded through the observability proxy"),
    "splash_obs_tokens_total": Counter(
        "splash_obs_tokens_total", "Tokens reported by the upstream model"),
    "splash_obs_tool_calls_total": Counter(
        "splash_obs_tool_calls_total", "Tool calls returned by the model"),
    "splash_obs_errors_total": Counter(
        "splash_obs_errors_total", "Upstream request errors by type"),
    "splash_obs_stream_requests_total": Counter(
        "splash_obs_stream_requests_total", "Streaming requests"),
    "splash_obs_client_disconnects_total": Counter(
        "splash_obs_client_disconnects_total", "Requests aborted by the client"),
    "splash_obs_cache_status_total": Counter(
        "splash_obs_cache_status_total", "Requests by upstream prompt-cache status"),
    "splash_obs_speculation_tokens_total": Counter(
        "splash_obs_speculation_tokens_total", "Speculative decoding draft/accepted tokens"),
}
GAUGES = {
    "splash_obs_upstream_available": Gauge(
        "splash_obs_upstream_available", "1 when Splash answered the last probe"),
    "splash_obs_in_flight": Gauge(
        "splash_obs_in_flight", "Requests currently in flight"),
}
HISTOGRAMS = {
    "splash_obs_request_duration_seconds": Histogram(
        "splash_obs_request_duration_seconds", "End-to-end request duration"),
    "splash_obs_ttft_seconds": Histogram(
        "splash_obs_ttft_seconds", "Time to first token/content delta (streaming only)"),
    "splash_obs_itl_seconds": Histogram(
        "splash_obs_itl_seconds", "Inter-token latency, mean gap between streamed chunks"),
    "splash_obs_input_chars": Histogram(
        "splash_obs_input_chars", "Prompt characters (metadata, not content)", CHAR_BUCKETS),
    "splash_obs_output_chars": Histogram(
        "splash_obs_output_chars", "Completion characters (metadata, not content)", CHAR_BUCKETS),
}


def proxy_metrics():
    parts = [GAUGES["splash_obs_upstream_available"].render(),
             GAUGES["splash_obs_in_flight"].render()]
    parts += [c.render() for c in COUNTERS.values()]
    parts += [h.render() for h in HISTOGRAMS.values()]
    return "\n".join(parts) + "\n"


# ------------------------------------------------------------ loki push ----

class LokiPusher:
    def __init__(self, url):
        self.url = url.rstrip("/") + "/loki/api/v1/push"
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def push(self, record):
        self.queue.put(record)

    def _worker(self):
        while True:
            batch = []
            try:
                batch.append(self.queue.get(timeout=5))
                while not self.queue.empty() and len(batch) < 100:
                    batch.append(self.queue.get_nowait())
            except queue.Empty:
                continue
            events = []
            for rec in batch:
                ts = rec.get("ts_unix_ns", int(time.time() * 1e9))
                # Loki 3.x wants {"stream": {labels}, "values": [[ts, line], ...]}.
                # The older flat {timestamp, line, labels} shape is accepted with a
                # 200 but stores nothing, so traces disappear silently.
                events.append({
                    "stream": {
                        "app": "splash-obs",
                        "kind": rec.get("kind", "request"),
                        "status_class": rec.get("status_class", "unknown"),
                        "stream": "true" if rec.get("stream") else "false",
                        "finish_reason": rec.get("finish_reason") or "none",
                    },
                    "values": [[str(ts), json.dumps(rec, separators=(",", ":"))]],
                })
            payload = json.dumps({"streams": events}).encode()
            try:
                req = urllib.request.Request(
                    self.url, data=payload,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:  # observability must never break inference
                log.warning("loki push failed: %s", exc)


LOKI = LokiPusher(LOKI_URL) if LOKI_URL else None

TRACE_LOCK = threading.Lock()


def _rotate_trace_log(path):
    """Size-based rotation, called with TRACE_LOCK held.

    path -> path.1 -> ... -> path.{TRACE_KEEP-1}: TRACE_KEEP files in total, oldest
    dropped.
    """
    oldest = "%s.%d" % (path, TRACE_KEEP - 1)
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError as exc:
            log.warning("trace rotation could not drop %s: %s", oldest, exc)
    for gen in range(TRACE_KEEP - 2, 0, -1):
        src = "%s.%d" % (path, gen)
        if os.path.exists(src):
            try:
                os.replace(src, "%s.%d" % (path, gen + 1))
            except OSError as exc:
                log.warning("trace rotation failed for %s: %s", src, exc)
    try:
        os.replace(path, path + ".1")
    except OSError as exc:
        log.warning("trace rotation failed for %s: %s", path, exc)


def append_trace(record, path=None, max_bytes=None):
    """Append one trace record.

    path/max_bytes fall back to the module settings, and are parameters so a test (or
    anything embedding the proxy) can write somewhere else without a reload.
    """
    path = path or TRACE_LOG
    limit = TRACE_MAX_BYTES if max_bytes is None else max_bytes
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with TRACE_LOCK:
            if limit > 0 and os.path.exists(path) and os.path.getsize(path) >= limit:
                _rotate_trace_log(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:
        log.warning("trace log write failed: %s", exc)
    if LOKI:
        LOKI.push(record)


# ------------------------------------------------------- request summarising --

def request_summary(body):
    """Metadata extracted from the request JSON. Content is never stored."""
    try:
        data = json.loads(body)
    except Exception:
        return {"malformed_json": True}
    messages = data.get("messages") or []
    tools = data.get("tools") or []
    response_format = data.get("response_format")
    if isinstance(response_format, dict):
        response_format = response_format.get("type")
    input_chars = 0
    image_parts = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            input_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        input_chars += len(str(part.get("text", "")))
                    elif part.get("type") in ("image_url", "image"):
                        image_parts += 1
    return {
        "model": data.get("model"),
        "stream": bool(data.get("stream", False)),
        "max_tokens": data.get("max_tokens") or data.get("max_completion_tokens"),
        "temperature": data.get("temperature"),
        "reasoning_effort": data.get("reasoning_effort"),
        "message_count": len(messages),
        "input_chars": input_chars,
        "image_parts": image_parts,
        "tool_count": len(tools),
        "tool_names": [t.get("function", {}).get("name") for t in tools][:30],
        "tool_choice": data.get("tool_choice"),
        "response_format": response_format,
    }


def iter_json_objects(chunk):
    """Yield JSON objects from a raw response chunk (SSE or plain JSON body).

    A single TCP chunk can carry several SSE lines, so parse line by line.
    """
    text = chunk.decode("utf-8", errors="replace")
    if not text.strip():
        return
    if text.lstrip().startswith("{"):
        try:
            yield json.loads(text)
            return
        except Exception:
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except Exception:
            continue


def usage_from(obj):
    usage = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def upstream_error_type(exc):
    """Classify a connection failure for the error_type label and the trace log.

    http.client throws bare OSError/ConnectionError for most transport failures, so
    timeouts and DNS failures have to be split out before the catch-all. Check
    str(exc), strerror and args: which of them carries the text varies by Python
    version (3.13 changed OSError's str() to include strerror).
    """
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, socket.herror):
        return "dns_error"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    text = " ".join(part for part in (
        str(exc), getattr(exc, "strerror", None) or "",
        " ".join(str(a) for a in getattr(exc, "args", ()) or ())))
    if "getaddrinfo" in text.lower() or "name or service not known" in text.lower():
        return "dns_error"
    return "connection_error"


def http_error_type(status, payload):
    """Extract OpenAI-compatible error.type from an upstream error body, else http_NNN."""
    try:
        for obj in iter_json_objects(payload):
            err = obj.get("error")
            if isinstance(err, dict):
                return err.get("type") or err.get("code") or "http_error"
            if isinstance(err, str):
                return err
    except Exception:
        pass
    return "http_%d" % status


# ----------------------------------------------------------- HTTP handler ----

class Handler(http.server.BaseHTTPRequestHandler):
    metrics_only = False
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- helpers ----
    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            return self.rfile.read(length)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while True:
                size_line = self.rfile.readline().strip()
                try:
                    size = int(size_line, 16)
                except ValueError:
                    break
                if size == 0:
                    break
                body += self.rfile.read(size)
                self.rfile.readline()
            return body
        return b""

    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _upstream(self, method, path, body):
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in ("host", "connection", "content-length",
                               "transfer-encoding", "accept-encoding"):
                continue
            headers[key] = value
        if UPSTREAM_HOST_HEADER:
            headers["Host"] = UPSTREAM_HOST_HEADER
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT,
                                         timeout=UPSTREAM_TIMEOUT)
        conn.request(method, path, body=body or None, headers=headers)
        return conn, conn.getresponse()

    # ---- routes ----
    def do_GET(self):
        path = self.path
        if path == "/healthz":
            self._json(200, {"status": "ok", "proxy": VERSION,
                             "upstream": "http://%s:%d" % (UPSTREAM_HOST, UPSTREAM_PORT)})
            return
        if path == "/metrics":
            self._send(200, proxy_metrics().encode(), "text/plain; version=0.0.4")
            return
        if path == "/metrics/splash":
            try:
                conn, resp = self._upstream("GET", "/metrics", None)
                payload = resp.read()
                GAUGES["splash_obs_upstream_available"].set({}, 1)
                conn.close()
                self._send(resp.status, payload, "text/plain; version=0.0.4")
            except Exception as exc:
                GAUGES["splash_obs_upstream_available"].set({}, 0)
                self._json(502, {"error": "upstream metrics unavailable",
                                 "detail": str(exc)})
            return
        if self.metrics_only:
            self._json(404, {"error": "metrics listener only serves /metrics paths"})
            return
        try:
            conn, resp = self._upstream("GET", path, None)
            payload = resp.read()
            conn.close()
            self._send(resp.status, payload,
                       resp.headers.get("Content-Type", "application/json"))
        except Exception as exc:
            self._json(502, {"error": "upstream request failed", "detail": str(exc)})

    def do_HEAD(self):
        # Same routing as GET, but a HEAD response must not carry a body. The
        # passthrough branch of do_GET writes one, so keep HEAD to the endpoints
        # we can answer without touching wfile.
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/metrics":
            body = proxy_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        elif self.metrics_only:
            self._json(404, {"error": "metrics listener only serves /metrics paths"})
        else:
            self._json(405, {"error": "HEAD is only supported on /healthz and /metrics"})

    def do_POST(self):
        # A local variable: the handler class is instantiated per connection, so this
        # must not become an attribute of self.
        in_flight = GAUGES["splash_obs_in_flight"]
        if self.metrics_only:
            # The metrics listener is read-only: a POST here would forward traffic to
            # Splash and write traces that bypass the client endpoint entirely.
            self._json(405, {"error": "metrics listener is read-only"})
            return

        body = self._read_body()
        summary = request_summary(body)
        model = summary.get("model") or "unknown"
        stream = bool(summary.get("stream"))
        started = time.monotonic()
        in_flight.add({}, 1)

        try:
            conn, resp = self._upstream("POST", self.path, body)
        except Exception as exc:
            error = upstream_error_type(exc)
            GAUGES["splash_obs_upstream_available"].set({}, 0)
            in_flight.add({}, -1)
            COUNTERS["splash_obs_errors_total"].inc({"model": model, "type": error})
            append_trace({
                "kind": "request", "ts": datetime.now(timezone.utc).isoformat(),
                "ts_unix_ns": int(time.time_ns()), "model": model, "stream": stream,
                "status": 502, "status_class": "5xx", "error_type": error,
                "error": str(exc)[:300], "duration_ms": (time.monotonic() - started) * 1000,
                "path": self.path, **summary,
            })
            self._json(502, {"error": {"message": "upstream unreachable",
                                       "type": error,
                                       "detail": str(exc)}})
            return

        content_type = resp.headers.get("Content-Type", "")
        # The response's own content type decides how we read it. `stream` only says
        # what the client asked for: upstream can answer a streaming request with a
        # plain JSON error, and chunk-reading that would hang until the socket dies.
        is_stream = "event-stream" in content_type
        ttft = None
        last_byte = None
        chunks = 0
        content_chars = 0
        reasoning_chars = 0
        tool_calls = 0
        finish_reasons = []
        usage = None
        error_type = None
        upstream_metrics = None
        response_id = None

        def fold(obj):
            nonlocal usage, upstream_metrics, response_id
            nonlocal content_chars, reasoning_chars, tool_calls
            if response_id is None:
                response_id = obj.get("id")
            found = usage_from(obj)
            if found:
                usage = found
            metrics = obj.get("metrics")
            if isinstance(metrics, dict):
                upstream_metrics = metrics
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or choice.get("message") or {}
                content_chars += len(delta.get("content") or "")
                reasoning_chars += len(delta.get("reasoning_content") or "")
                tool_calls += len(delta.get("tool_calls") or [])
                if choice.get("finish_reason"):
                    finish_reasons.append(choice["finish_reason"])

        if is_stream:
            self.send_response(resp.status)
            self.send_header("Content-Type", content_type or "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in resp.headers.items():
                if key.lower() in ("content-type", "content-length", "transfer-encoding",
                                   "connection", "date"):
                    continue
                self.send_header(key, value)
            self.end_headers()
            done = False
            try:
                while not done:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
                    if not chunk:
                        break
                    last_byte = time.monotonic()
                    chunks += 1
                    for obj in iter_json_objects(chunk):
                        fold(obj)
                        err = obj.get("error") if isinstance(obj, dict) else None
                        if err:
                            error_type = ((err.get("type") or err.get("code"))
                                          if isinstance(err, dict) else None) or "stream_error"
                    if ttft is None and (content_chars or reasoning_chars or tool_calls):
                        ttft = time.monotonic()
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    # Splash ends a stream with `data: [DONE]` and then keeps the
                    # connection alive for reuse. Waiting for EOF would block on every
                    # streaming request until the upstream timeout (up to 600s),
                    # inflating duration and delaying the trace record.
                    if b"[DONE]" in chunk:
                        done = True
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                COUNTERS["splash_obs_client_disconnects_total"].inc({"model": model})
                error_type = "client_disconnect"
            finally:
                conn.close()
        else:
            payload = resp.read()
            conn.close()
            first_byte = time.monotonic()
            ttft = first_byte
            for obj in iter_json_objects(payload):
                fold(obj)
            if resp.status >= 400:
                error_type = http_error_type(resp.status, payload)
            self._send(resp.status, payload, content_type or "application/json")

        duration = time.monotonic() - started
        GAUGES["splash_obs_upstream_available"].set({}, 1)
        in_flight.add({}, -1)

        ttft_s = (ttft - started) if ttft is not None else None
        # Inter-token latency: mean gap between chunks after the first token.
        itl_s = None
        if stream and chunks > 1 and ttft is not None and last_byte is not None:
            itl_s = (last_byte - ttft) / (chunks - 1)

        status_class = "%dxx" % (resp.status // 100)
        COUNTERS["splash_obs_requests_total"].inc({
            "model": model, "status_class": status_class, "stream": "true" if stream else "false"})
        if stream:
            COUNTERS["splash_obs_stream_requests_total"].inc({"model": model})
        if resp.status >= 400:
            COUNTERS["splash_obs_errors_total"].inc({"model": model, "type": error_type or "http_%d" % resp.status})
        if usage:
            COUNTERS["splash_obs_tokens_total"].inc({"model": model, "kind": "prompt"}, usage.get("prompt_tokens") or 0)
            COUNTERS["splash_obs_tokens_total"].inc({"model": model, "kind": "completion"}, usage.get("completion_tokens") or 0)
        if tool_calls:
            COUNTERS["splash_obs_tool_calls_total"].inc({"model": model}, tool_calls)
        HISTOGRAMS["splash_obs_request_duration_seconds"].observe({"model": model}, duration)
        # TTFT is only recorded for streams: a non-stream response has no first token
        # separate from the whole body, so observing it would just double-count duration
        # into the same p95. It is still reported per request in the trace log, where
        # the `stream` field lets you filter.
        if stream and ttft_s is not None:
            HISTOGRAMS["splash_obs_ttft_seconds"].observe({"model": model}, ttft_s)
        if itl_s is not None:
            HISTOGRAMS["splash_obs_itl_seconds"].observe({"model": model}, itl_s)
        HISTOGRAMS["splash_obs_input_chars"].observe({"model": model}, summary.get("input_chars", 0))
        HISTOGRAMS["splash_obs_output_chars"].observe({"model": model}, content_chars)

        record = {
            "kind": "request",
            "ts": datetime.now(timezone.utc).isoformat(),
            "ts_unix_ns": int(time.time_ns()),
            "response_id": response_id,
            "path": self.path,
            "model": model,
            "stream": stream,
            "status": resp.status,
            "status_class": status_class,
            "duration_ms": round(duration * 1000, 2),
            "ttft_ms": round(ttft_s * 1000, 2) if ttft_s is not None else None,
            "itl_ms": round(itl_s * 1000, 2) if itl_s is not None else None,
            "chunks": chunks,
            "output_chars": content_chars,
            "reasoning_chars": reasoning_chars,
            "tool_calls": tool_calls,
            "finish_reason": finish_reasons[0] if finish_reasons else None,
            "usage": usage,
            "error_type": error_type,
            "client": self.address_string(),
        }
        if upstream_metrics:
            prefill = upstream_metrics.get("prefill") or {}
            decode = upstream_metrics.get("decode") or {}
            latency = upstream_metrics.get("request_latency") or {}
            cache = upstream_metrics.get("cache") or {}
            speculation = upstream_metrics.get("speculation") or {}
            record["upstream"] = {
                "prefill_tokens": prefill.get("tokens"),
                "decode_tokens": decode.get("tokens"),
                "ttft_ms": latency.get("ttft_ms"),
                "wall_ms": latency.get("wall_ms"),
                "cache_status": cache.get("status"),
                "cache_matched_tokens": cache.get("matched_tokens"),
                "drafted_tokens": speculation.get("drafted_tokens"),
                "accepted_draft_tokens": speculation.get("accepted_draft_tokens"),
                "speculation_cycles": speculation.get("cycles"),
            }
            if cache.get("status"):
                COUNTERS["splash_obs_cache_status_total"].inc(
                    {"model": model, "status": cache["status"]})
            if speculation:
                COUNTERS["splash_obs_speculation_tokens_total"].inc(
                    {"model": model, "kind": "drafted"}, speculation.get("drafted_tokens", 0))
                COUNTERS["splash_obs_speculation_tokens_total"].inc(
                    {"model": model, "kind": "accepted"},
                    speculation.get("accepted_draft_tokens", 0))
        record.update({k: v for k, v in summary.items() if k not in ("model", "stream")})
        append_trace(record)
        log.info("%s %s -> %d  %dms  ttft=%s  out=%d chars  tools=%d",
                 self.command, self.path, resp.status, duration * 1000,
                 record["ttft_ms"], content_chars, tool_calls)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    os.makedirs(os.path.dirname(TRACE_LOG) or ".", exist_ok=True)

    client_handler = type("ClientHandler", (Handler,), {"metrics_only": False})
    metrics_handler = type("MetricsHandler", (Handler,), {"metrics_only": True})

    servers = []
    for host, port, handler in (
        (CLIENT_HOST, CLIENT_PORT, client_handler),
        (METRICS_HOST, METRICS_PORT, metrics_handler),
    ):
        try:
            srv = http.server.ThreadingHTTPServer((host, port), handler)
            servers.append(srv)
            log.info("listening on http://%s:%d", host, port)
        except OSError as exc:
            log.error("failed to bind %s:%d - %s", host, port, exc)
            for s in servers:
                s.server_close()
            sys.exit(1)

    # serve_forever blocks, so every listener but the first needs its own thread.
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        for srv in servers:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    main()
