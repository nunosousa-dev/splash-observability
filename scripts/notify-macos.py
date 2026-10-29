#!/usr/bin/env python3
"""Alertmanager webhook receiver that raises a macOS notification.

Runs on the Mac (not in a container) because the notification has to appear on the
desktop. Alertmanager reaches it through host.docker.internal.

  ./scripts/notify-macos.py                 # listen on 127.0.0.1:9393
  ./scripts/notify-macos.py --test          # send one notification and exit

Alertmanager re-posts a group whenever it changes, so the same alert can arrive more
than once. Notifications are deduplicated per (alertname, state) for
SPLASH_NOTIFY_DEDUPE seconds (default 600) so a flapping alert cannot spam the desktop.

The alert text is passed to osascript as an argv element, never interpolated into
AppleScript source, so nothing in an alert can inject script.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("SPLASH_NOTIFY_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPLASH_NOTIFY_PORT", "9393"))
DEDUPE_SECONDS = float(os.environ.get("SPLASH_NOTIFY_DEDUPE", 600))

log = logging.getLogger("splash-notify")
_seen = {}               # (alertname, state) -> last notified unix time


def notify(title, message):
    """Raise a macOS notification. Returns True if osascript ran."""
    script = ("on run {t, m}\n"
              "  display notification m with title t sound name \"Glass\"\n"
              "end run")
    try:
        subprocess.run(["osascript", "-e", script, title, message],
                       timeout=15, check=False, capture_output=True)
    except FileNotFoundError:
        log.error("osascript not found; this notifier only works on macOS")
        return False
    except subprocess.TimeoutExpired:
        log.warning("osascript timed out")
        return False
    return True


def should_send(key, now):
    """True if this (alertname, state) has not been notified recently."""
    last = _seen.get(key)
    if last is not None and now - last < DEDUPE_SECONDS:
        return False
    _seen[key] = now
    return True


def handle_alerts(payload):
    """Fire one notification per alert in the batch, deduplicated. Returns the count."""
    sent = 0
    now = time.time()
    for alert in payload.get("alerts") or []:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        name = labels.get("alertname", "alert")
        state = alert.get("status", "firing")
        if not should_send((name, state), now):
            log.debug("suppressed duplicate (%s, %s)", name, state)
            continue
        summary = annotations.get("summary") or name
        detail = annotations.get("description") or ""
        message = "%s · %s" % (summary, labels.get("severity", "-"))
        if detail:
            message = "%s — %s" % (message, detail)
        notify("%s %s" % ("FIRING" if state == "firing" else "OK", name),
               message[:400])
        sent += 1
    return sent


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._reply(200, {"status": "ok", "listener": "splash-notify",
                          "dedupe_seconds": DEDUPE_SECONDS})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError as exc:
            self._reply(400, {"error": "invalid json", "detail": str(exc)})
            return
        if self.path.startswith("/test"):
            notify("Splash test", "notification path is working")
            self._reply(200, {"ok": True})
            return
        sent = handle_alerts(payload)
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
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
