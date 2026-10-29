#!/usr/bin/env python3
"""Alertmanager webhook receiver that raises a macOS notification.

Runs on the Mac (not in a container) because the notification has to appear on the
desktop. Alertmanager reaches it through host.docker.internal.

  ./scripts/notify-macos.py                 # listen on 127.0.0.1:9393
  ./scripts/notify-macos.py --test          # send one notification and exit

Only critical alerts notify. Repeated deliveries are deduplicated per alert label
set and state for SPLASH_NOTIFY_DEDUPE seconds (default 600). A resolved alert and
a subsequent re-fire both notify, even inside that window.

The alert text is passed to osascript as an argv element, never interpolated into
AppleScript source, so nothing in an alert can inject script.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("SPLASH_NOTIFY_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPLASH_NOTIFY_PORT", "9393"))
DEDUPE_SECONDS = float(os.environ.get("SPLASH_NOTIFY_DEDUPE", 600))

log = logging.getLogger("splash-notify")
_seen = {}               # sorted alert labels -> (last state, monotonic timestamp)
_seen_lock = threading.Lock()
MAX_BODY_BYTES = 1024 * 1024


class NotificationError(RuntimeError):
    pass


def notify(title, message):
    """Ask macOS to display a notification; OS settings still control visibility."""
    script = ("on run {t, m}\n"
              "  display notification m with title t sound name \"Glass\"\n"
              "end run")
    try:
        result = subprocess.run(["osascript", "-e", script, "--", title, message],
                                timeout=15, check=False, capture_output=True)
    except FileNotFoundError:
        log.error("osascript not found; this notifier only works on macOS")
        return False
    except subprocess.TimeoutExpired:
        log.warning("osascript timed out")
        return False
    if result.returncode:
        log.error("osascript failed (exit %d): %s", result.returncode,
                  result.stderr.decode("utf-8", errors="replace").strip())
        return False
    return True


def validate_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        raise ValueError("expected an object with an alerts array")
    for alert in payload["alerts"]:
        if not isinstance(alert, dict) or alert.get("status") not in ("firing", "resolved"):
            raise ValueError("each alert needs status firing or resolved")
        for field in ("labels", "annotations"):
            values = alert.get(field, {})
            if not isinstance(values, dict) or any(
                    not isinstance(value, str) for value in values.values()):
                raise ValueError("alert %s must be a string map" % field)


def handle_alerts(payload):
    """Notify critical alerts. Failed deliveries return 503 so Alertmanager retries."""
    validate_payload(payload)
    sent = 0
    # Serialize batches so concurrent webhook requests cannot bypass deduplication.
    with _seen_lock:
        now = time.monotonic()
        for key, (_, timestamp) in list(_seen.items()):
            if now - timestamp >= DEDUPE_SECONDS:
                del _seen[key]
        for alert in payload["alerts"]:
            labels = alert.get("labels", {})
            if labels.get("severity") != "critical":
                continue
            annotations = alert.get("annotations", {})
            name = labels.get("alertname", "alert")
            state = alert["status"]
            key = tuple(sorted(labels.items()))
            previous = _seen.get(key)
            if previous and previous[0] == state:
                continue
            summary = annotations.get("summary") or name
            detail = annotations.get("description") or ""
            message = "%s · critical" % summary
            if detail:
                message = "%s — %s" % (message, detail)
            if not notify("%s %s" % ("FIRING" if state == "firing" else "OK", name),
                          message[:400]):
                # Do not mark the failed alert as sent. Earlier successes in this
                # batch remain deduped when Alertmanager retries the whole batch.
                raise NotificationError("desktop notification failed")
            _seen[key] = (state, time.monotonic())
            sent += 1
    return sent


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path not in ("/", "/healthz"):
            self._reply(404, {"error": "not found"})
            return
        self._reply(200, {"status": "ok", "listener": "splash-notify",
                          "dedupe_seconds": DEDUPE_SECONDS})

    def do_POST(self):
        if self.path != "/alert":
            self.close_connection = True
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_BODY_BYTES:
                self.close_connection = True
                self._reply(413, {"error": "body must be between 1 byte and 1 MiB"})
                return
            payload = json.loads(self.rfile.read(length))
            sent = handle_alerts(payload)
        except (ValueError, UnicodeError) as exc:
            self.close_connection = True
            self._reply(400, {"error": "invalid json", "detail": str(exc)})
            return
        except NotificationError as exc:
            self._reply(503, {"error": str(exc)})
            return
        log.info("POST %s: %d alert(s), %d notification(s)", self.path,
                 len(payload.get("alerts") or []), sent)
        self._reply(200, {"ok": True, "notified": sent})

    def _reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send one notification and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.test:
        return 0 if notify("Splash test", "notification path is working") else 1

    log.info("listening on http://%s:%d (dedupe window %.0fs)",
             HOST, PORT, DEDUPE_SECONDS)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
