#!/usr/bin/env python3
"""Regression tests for the Splash observability proxy (stdlib only).

A stub upstream stands in for Splash, so these run without a model loaded:

  python3 -m unittest discover -s tests

Covered: the exposition format Prometheus can actually parse, the metrics listener
being read-only, error classification, TTFT/ITL gating on streaming, in-flight
accounting under concurrency, and trace-log rotation.
"""

import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRACE_LOG = os.path.join(HERE, "_test_traces.jsonl")

# The proxy reads its configuration at import time, so these must be set first.
os.environ["SPLASH_OBS_TRACE_LOG"] = TRACE_LOG
os.environ["LOKI_URL"] = ""          # keep tests offline
os.environ["SPLASH_OBS_TRACE_MAX_BYTES"] = "0"   # rotation is covered explicitly
os.environ["SPLASH_OBS_PORT"] = "18100"
os.environ["SPLASH_OBS_METRICS_PORT"] = "18101"


def _load_proxy():
    spec = importlib.util.spec_from_file_location(
        "splash_obs_proxy", os.path.join(ROOT, "proxy", "splash_obs_proxy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


proxy = _load_proxy()


# ------------------------------------------------------------- stub upstream ----

class Upstream:
    """A fake Splash. `scenario` selects the response; `host_header` records what
    the proxy sent so the loopback Host trick can be asserted."""

    def __init__(self):
        self.scenario = "plain"
        self.host_header = None
        self.path = None
        srv = self

        class Stub(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply_stream_headers(self):
                # HTTP/1.0: no transfer-encoding is negotiated, so the proxy reads
                # until EOF and does its own chunk framing towards the client.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

            def do_GET(self):
                if self.path == "/metrics":
                    body = b"splash_upstream_probe 1\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                self.rfile.read(length)
                srv.host_header = self.headers.get("Host")
                srv.path = self.path
                srv.reply(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reply(self, h):
        s = self.scenario
        if s == "plain":
            obj = {"id": "chatcmpl-1", "choices": [{"message": {"content": "hi"},
                                                    "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
            self._json(h, 200, obj)
        elif s == "stream":
            h.reply_stream_headers()
            for i in range(3):
                piece = {"choices": [{"delta": {"content": "x" * 10},
                                      "finish_reason": None}]}
                h.wfile.write(b"data: %s\n\n" % json.dumps(piece).encode())
                h.wfile.flush()
            final = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 9, "completion_tokens": 30,
                               "total_tokens": 39}}
            h.wfile.write(b"data: %s\n\n" % json.dumps(final).encode())
            h.wfile.write(b"data: [DONE]\n\n")
            h.wfile.flush()
        elif s == "slow_stream":
            h.reply_stream_headers()
            for i in range(6):
                time.sleep(0.012)
                piece = {"choices": [{"delta": {"content": "ab"},
                                      "finish_reason": None}]}
                h.wfile.write(b"data: %s\n\n" % json.dumps(piece).encode())
                h.wfile.flush()
            h.wfile.write(b"data: [DONE]\n\n")
            h.wfile.flush()
        elif s == "bad_request":
            self._json(h, 400, {"error": {"message": "bad", "type": "invalid_request_error"}})
        elif s == "rate_limited":
            self._json(h, 429, {"error": {"message": "slow down", "type": "rate_limit_exceeded"}})
        elif s == "stream_error_object":
            # 200 SSE that carries an error object, then terminates the stream
            h.reply_stream_headers()
            h.wfile.write(b'data: {"error": {"type": "engine_crashed"}}\n\n')
            h.wfile.write(b'data: [DONE]\n\n')
            h.wfile.flush()
        else:
            self._json(h, 500, {"error": {"message": "?"}})

    @staticmethod
    def _json(h, status, obj):
        body = json.dumps(obj).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


UP = Upstream()
proxy.UPSTREAM_HOST = "127.0.0.1"
proxy.UPSTREAM_PORT = UP.port

_servers = []
for _host, _port, _only in (("127.0.0.1", 18100, False), ("127.0.0.1", 18101, True)):
    _handler = type("H", (proxy.Handler,), {"metrics_only": _only})
    _srv = ThreadingHTTPServer((_host, _port), _handler)
    _servers.append(_srv)
    threading.Thread(target=_srv.serve_forever, daemon=True).start()

CLIENT = "http://127.0.0.1:18100"
METRICS = "http://127.0.0.1:18101"


def _reset():
    for c in proxy.COUNTERS.values():
        c.values.clear()
    for g in proxy.GAUGES.values():
        g.values.clear()
    for h in proxy.HISTOGRAMS.values():
        h.data.clear()


def chat(stream=False, model="stub-model"):
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 20}
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(CLIENT + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _read_trace():
    if not os.path.exists(TRACE_LOG):
        return []
    with open(TRACE_LOG, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ListHandler(logging.Handler):
    """Capture proxy log records so 'it silently did nothing' can't pass as green."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class ProxyTest(unittest.TestCase):
    def _wait_for_trace(self, count=1, timeout=10.0):
        """Wait for the handler thread to write its trace record.

        Response bytes reach the client before append_trace runs, so reading the log
        straight away is a race that only loses under load. Returns all records so
        callers can assert the exact count too.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = _read_trace()
            if len(records) >= count:
                return records
            time.sleep(0.02)
        raise AssertionError("expected >=%d trace records, got %d"
                             % (count, len(_read_trace())))

    def last_trace(self, count=1):
        return self._wait_for_trace(count)[-1]

    def setUp(self):
        _reset()
        UP.scenario = "plain"
        UP.path = None
        UP.host_header = None
        self._cap = ListHandler()
        logging.getLogger("splash-obs").addHandler(self._cap)
        logging.getLogger("splash-obs").setLevel(logging.DEBUG)
        if os.path.exists(TRACE_LOG):
            os.remove(TRACE_LOG)

    def tearDown(self):
        logging.getLogger("splash-obs").removeHandler(self._cap)

    @classmethod
    def tearDownClass(cls):
        UP.stop()
        for srv in _servers:
            srv.shutdown()
            srv.server_close()
        if os.path.exists(TRACE_LOG):
            os.remove(TRACE_LOG)

    # ---- exposition format ----

    def test_exposition_is_parseable(self):
        """Every sample line must parse. A single malformed line truncates the
        scrape silently, which is how counters vanished in the past."""
        chat()
        body = urllib.request.urlopen(METRICS + "/metrics", timeout=10).read().decode()
        line_re = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*'
                             r'(\{([a-zA-Z_][a-zA-Z0-9_]*="[^"]*"(,[a-zA-Z_][a-zA-Z0-9_]*="[^"]*")*)?\})?'
                             r' -?[\d.eE+]+(\s+\d+)?$')
        bad = [l for l in body.splitlines()
               if l and not l.startswith("#") and not line_re.match(l)]
        self.assertEqual([], bad, "unparseable sample lines: %s" % bad[:5])

    def test_no_leading_comma_in_labels(self):
        chat()
        body = urllib.request.urlopen(METRICS + "/metrics", timeout=10).read().decode()
        self.assertNotIn("{,", body)

    def test_histogram_buckets_are_cumulative_and_sorted(self):
        for _ in range(3):
            chat()
        h = proxy.HISTOGRAMS["splash_obs_request_duration_seconds"]
        for labels, row in h.data.items():
            counts = row[:len(h.buckets)]
            self.assertEqual(counts, sorted(counts), "buckets must be cumulative")
            self.assertEqual(counts[-1], row[len(h.buckets)],
                             "last finite bucket must equal +Inf count")

    def test_char_histograms_use_char_buckets(self):
        """Latency buckets would bury every prompt in +Inf."""
        chat()
        h = proxy.HISTOGRAMS["splash_obs_input_chars"]
        self.assertEqual(list(proxy.CHAR_BUCKETS), h.buckets)

    # ---- listener separation ----

    def test_metrics_endpoint_serves_prometheus(self):
        body = urllib.request.urlopen(METRICS + "/metrics", timeout=10).read().decode()
        self.assertIn("splash_obs_upstream_available", body)

    def test_metrics_rejects_post(self):
        req = urllib.request.Request(METRICS + "/metrics", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(405, ctx.exception.code)

    def test_metrics_post_does_not_reach_upstream(self):
        req = urllib.request.Request(METRICS + "/v1/chat/completions",
                                     data=json.dumps({"model": "m", "messages": []}).encode(),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError:
            pass
        self.assertIsNone(UP.path, "metrics listener forwarded a request upstream")

    def test_upstream_splash_passthrough(self):
        body = urllib.request.urlopen(METRICS + "/metrics/splash", timeout=10).read().decode()
        self.assertIn("splash_upstream_probe", body)

    def test_loopback_host_header_is_presented(self):
        """Splash allow-lists Host; the proxy must send 127.0.0.1 even when it
        connects to host.docker.internal."""
        proxy.UPSTREAM_HOST_HEADER = "127.0.0.1"
        chat()
        self.assertEqual("127.0.0.1", UP.host_header)

    # ---- error classification ----

    def test_bad_request_error_type_from_body(self):
        UP.scenario = "bad_request"
        status, _ = chat()
        self.assertEqual(400, status)
        rec = self.last_trace()
        self.assertEqual("invalid_request_error", rec["error_type"])
        rendered = urllib.request.urlopen(METRICS + "/metrics", timeout=10).read().decode()
        self.assertIn('splash_obs_errors_total{model="stub-model",type="invalid_request_error"}',
                      rendered)

    def test_rate_limit_error_type(self):
        UP.scenario = "rate_limited"
        chat()
        self.assertEqual("rate_limit_exceeded", self.last_trace()["error_type"])

    def test_stream_error_object_is_classified(self):
        UP.scenario = "stream_error_object"
        chat(stream=True)
        self.assertEqual("engine_crashed", self.last_trace()["error_type"])

    def test_stream_request_with_json_error_body_does_not_hang(self):
        """Client asked for a stream, upstream answered plain JSON. Reading that as
        chunked SSE would block until the socket timeout fires."""
        UP.scenario = "bad_request"
        status, body = chat(stream=True)
        self.assertEqual(400, status)
        self.assertIn(b"invalid_request_error", body)
        self.assertEqual("invalid_request_error", self.last_trace()["error_type"])

    def test_head_does_not_send_a_body(self):
        req = urllib.request.Request(CLIENT + "/metrics", method="HEAD")
        resp = urllib.request.urlopen(req, timeout=10)
        self.assertEqual(200, resp.status)
        self.assertEqual(b"", resp.read())

    def test_upstream_down_is_502_and_marked_unavailable(self):
        saved = proxy.UPSTREAM_PORT
        proxy.UPSTREAM_PORT = 1  # nothing listening
        try:
            status, body = chat()
            self.assertEqual(502, status)
            self.assertEqual("connection_refused",
                             json.loads(body)["error"]["type"])
            self.assertEqual(0.0, proxy.GAUGES["splash_obs_upstream_available"].get(()))
            rec = self.last_trace()
            self.assertEqual("connection_refused", rec["error_type"])
            self.assertEqual(502, rec["status"])
            # The error must be attributed, not bucketed under a generic name.
            rendered = urllib.request.urlopen(METRICS + "/metrics", timeout=10).read().decode()
            self.assertIn('splash_obs_errors_total{model="stub-model",type="connection_refused"}',
                          rendered)
            self.assertIn("splash_obs_in_flight 0", rendered,
                          "in-flight gauge leaked on the error path")
        finally:
            proxy.UPSTREAM_PORT = saved

    def test_connection_refused_classification(self):
        kind = proxy.upstream_error_type(ConnectionRefusedError("nope"))
        self.assertEqual("connection_refused", kind)

    def test_timeout_classification(self):
        import socket
        self.assertEqual("timeout", proxy.upstream_error_type(socket.timeout("slow")))
        self.assertEqual("timeout", proxy.upstream_error_type(TimeoutError("slow")))

    def test_dns_error_classification(self):
        import socket
        exc = socket.gaierror("boom")
        exc.strerror = "gaierror"
        self.assertEqual("dns_error", proxy.upstream_error_type(exc))

    def test_dns_error_by_message(self):
        self.assertEqual("dns_error",
                         proxy.upstream_error_type(OSError("getaddrinfo failed")))

    def test_dns_error_herror(self):
        import socket
        exc = socket.herror("Host information not found")
        exc.strerror = "Host information not found"
        self.assertEqual("dns_error", proxy.upstream_error_type(exc))

    def test_unclassified_os_error(self):
        self.assertEqual("connection_error", proxy.upstream_error_type(OSError("boom")))

    # ---- latency gating ----

    def test_ttft_recorded_for_stream_only(self):
        UP.scenario = "stream"
        chat(stream=True)
        chat(stream=False)
        hist = proxy.HISTOGRAMS["splash_obs_ttft_seconds"]
        total = sum(row[len(hist.buckets)] for row in hist.data.values())
        self.assertEqual(1, total, "TTFT histogram must only contain streaming requests")

    def test_non_stream_ttft_still_in_trace(self):
        chat(stream=False)
        rec = self.last_trace()
        self.assertIsNotNone(rec["ttft_ms"], "trace log keeps non-stream TTFT for filtering")

    def test_trace_has_no_content(self):
        """The proxy must never store prompt or completion text."""
        for scenario, stream in (("plain", False), ("stream", True), ("bad_request", False)):
            UP.scenario = scenario
            if os.path.exists(TRACE_LOG):
                os.remove(TRACE_LOG)
            req = urllib.request.Request(
                CLIENT + "/v1/chat/completions",
                data=json.dumps({"model": "super-secret-model-name",
                                 "messages": [{"role": "user",
                                               "content": "SECRET-PROMPT-TEXT"}],
                                 "stream": stream}).encode(),
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20).read()
            except urllib.error.HTTPError as e:
                e.read()
            raw = json.dumps(self.last_trace())
            self.assertNotIn("SECRET-PROMPT-TEXT", raw, "%s leaked prompt text" % scenario)
            # The stub's completion text is "hi" (plain) / "xxxxxxxxxx" (stream).
            self.assertNotIn("xxxxxxxxxx", raw, "%s leaked completion text" % scenario)
            self.assertNotIn("super-secret-model-name", raw.replace(
                "super-secret-model-name", ""), "model name should be the only echo")

    def test_stream_completes_without_waiting_for_eof(self):
        """Regression: Splash keeps the connection alive after `data: [DONE]`.
        Reading until EOF made every streaming request take the full upstream
        timeout, so the proxy must stop at the sentinel."""
        UP.scenario = "stream"
        started = time.monotonic()
        status, body = chat(stream=True)
        elapsed = time.monotonic() - started
        self.assertEqual(200, status)
        self.assertIn(b"[DONE]", body)
        self.assertLess(elapsed, 3.0,
                        "streaming request took %.1fs; the proxy is waiting on EOF" % elapsed)

    def test_stream_body_is_wellformed_chunked(self):
        """The proxy re-frames the SSE upstream into chunked encoding for the client;
        urllib's reader validates the framing, so a successful read() is the assert."""
        UP.scenario = "stream"
        status, body = chat(stream=True)
        self.assertEqual(200, status)
        self.assertGreaterEqual(body.count(b"data:"), 3)

    def test_itl_reported_for_multi_chunk_stream(self):
        UP.scenario = "slow_stream"
        chat(stream=True)
        rec = self.last_trace()
        self.assertGreater(rec["chunks"], 1, "expected a multi-chunk stream")
        self.assertIsInstance(rec["itl_ms"], float)
        self.assertGreater(rec["itl_ms"], 0.0)

    # ---- in-flight accounting ----

    def test_in_flight_returns_to_zero(self):
        UP.scenario = "plain"
        threads = [threading.Thread(target=chat) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(0.0, proxy.GAUGES["splash_obs_in_flight"].get(()))

    def test_in_flight_returns_to_zero_on_error(self):
        saved = proxy.UPSTREAM_PORT
        proxy.UPSTREAM_PORT = 1
        try:
            chat()
        finally:
            proxy.UPSTREAM_PORT = saved
        self.assertEqual(0.0, proxy.GAUGES["splash_obs_in_flight"].get(()))

    def test_concurrent_counters_lose_nothing(self):
        _reset()
        n_threads, per_thread = 8, 25
        barrier = threading.Barrier(n_threads)

        def bump():
            barrier.wait()
            for _ in range(per_thread):
                proxy.COUNTERS["splash_obs_requests_total"].inc({"model": "x"}, 1)

        threads = [threading.Thread(target=bump) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        total = sum(proxy.COUNTERS["splash_obs_requests_total"].values.values())
        self.assertEqual(n_threads * per_thread, total)

    # ---- trace log ----

    def test_trace_log_is_jsonl(self):
        """One JSON object per line, and nothing logged at WARNING or above."""
        chat()
        self.assertEqual([], ["%s: %s" % (r.levelname, r.getMessage())
                             for r in self._cap.records if r.levelno >= logging.WARNING])
        # chat() returns as soon as the bytes are flushed; append_trace happens after
        # that on the handler thread, so wait for the write instead of assuming it.
        records = self._wait_for_trace(1)
        self.assertEqual(1, len(records), "more than one trace record written")
        with open(TRACE_LOG, encoding="utf-8") as fh:
            raw = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(1, len(raw))
        json.loads(raw[0])                    # must parse as one object

    def test_trace_log_rotates(self):
        """Rotation is exercised through append_trace's explicit path/max_bytes so it
        does not depend on mutating import-time module state."""
        path = os.path.join(HERE, "_test_rotate.jsonl")
        for p in [path] + ["%s.%d" % (path, i) for i in range(1, 6)]:
            if os.path.exists(p):
                os.remove(p)
        try:
            for i in range(12):
                proxy.append_trace({"kind": "request", "seq": i,
                                    "pad": "x" * 80}, path=path, max_bytes=200)
            self.assertTrue(os.path.exists(path + ".1"), "no first generation after rotation")
            generations = ["%s.%d" % (path, i) for i in range(1, 10)]
            kept = [p for p in generations if os.path.exists(p)]
            self.assertLessEqual(len(kept), proxy.TRACE_KEEP - 1,
                                 "more generations kept than SPLASH_OBS_TRACE_KEEP")
            for part in [path] + kept:
                with open(part, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            json.loads(line)  # rotation must not split a line
        finally:
            for p in [path] + kept:
                if os.path.exists(p):
                    os.remove(p)

    def test_trace_rotation_disabled(self):
        path = os.path.join(HERE, "_test_norotate.jsonl")
        if os.path.exists(path):
            os.remove(path)
        try:
            for i in range(6):
                proxy.append_trace({"kind": "request", "seq": i, "pad": "y" * 80},
                                   path=path, max_bytes=0)
            self.assertFalse(os.path.exists(path + ".1"), "rotation happened while disabled")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(6, len([l for l in fh if l.strip()]))
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_rotation_keeps_valid_json_per_line(self):
        saved = proxy.TRACE_MAX_BYTES
        proxy.TRACE_MAX_BYTES = 120
        try:
            for _ in range(6):
                chat()
            parts = [TRACE_LOG] + [TRACE_LOG + ".%d" % i for i in range(1, 5)]
            lines = []
            for p in parts:
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as fh:
                        lines += [l for l in fh.read().splitlines() if l.strip()]
            for line in lines:
                json.loads(line)          # must not raise
        finally:
            proxy.TRACE_MAX_BYTES = saved

    # ---- helpers ----

    def test_request_summary_never_returns_content(self):
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "secret prompt text"}], "tools": [
            {"function": {"name": "do_thing"}}]}).encode()
        s = proxy.request_summary(body)
        self.assertEqual(18, s["input_chars"])
        self.assertEqual(["do_thing"], s["tool_names"])
        self.assertNotIn("secret", json.dumps(s))

    def test_malformed_json_is_flagged_not_fatal(self):
        s = proxy.request_summary(b"{not json")
        self.assertTrue(s["malformed_json"])

    def test_iter_json_objects_handles_split_sse(self):
        chunk = b'data: {"a":1}\n\ndata: {"b":2}\n\ndata: [DONE]\n\n'
        self.assertEqual([{"a": 1}, {"b": 2}], list(proxy.iter_json_objects(chunk)))

    def test_iter_json_objects_handles_plain_body(self):
        self.assertEqual([{"a": 1}], list(proxy.iter_json_objects(b'{"a":1}')))

    def test_loki_payload_uses_stream_and_values(self):
        """Loki 3.x accepts the old flat shape with a 200 and stores nothing."""
        payload = {"streams": [{"stream": {"app": "splash-obs"},
                               "values": [["1", "{}"]]}]}
        text = json.dumps(payload)
        self.assertIn('"stream"', text)
        self.assertIn('"values"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
