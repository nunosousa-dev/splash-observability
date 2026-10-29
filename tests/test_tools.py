"""Offline regression tests for smoke reporting, notification delivery and verification."""

from contextlib import redirect_stdout
from copy import deepcopy
from http.server import ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load("smoke", "smoke.py")
with patch.dict(sys.modules, {"smoke": smoke}):
    verify = load("verify", "verify.py")
notify = load("notify_macos", "notify-macos.py")

ALERT = {"status": "firing", "labels": {"alertname": "SplashUnreachable",
         "severity": "critical", "instance": "proxy:8101"},
         "annotations": {"summary": "Test alert", "description": "Test only"}}


class SmokeTest(unittest.TestCase):
    def test_usage_frame_without_choices_does_not_erase_finish_reason(self):
        events = [{"choices": [{"delta": {"content": "ok"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}]
        body = b"".join(b"data: " + json.dumps(obj).encode() + b"\n\n" for obj in events)
        result = smoke.response_summary(body + b"data: [DONE]\n\n", "text/event-stream; charset=utf-8")
        self.assertEqual("stop", result["finish_reason"])
        self.assertEqual(2, result["usage"]["completion_tokens"])

    def test_sse_comments_crlf_and_multiline_data(self):
        body = (b': keepalive\r\ndata: {"choices": [],\r\n'
                b'data: "usage": {"total_tokens": 11}}\r\n\r\ndata: [DONE]\r\n\r\n')
        self.assertEqual({"total_tokens": 11},
                         smoke.response_summary(body, "text/event-stream")["usage"])

    def test_plain_json(self):
        body = json.dumps({"choices": [{"finish_reason": "tool_calls"}],
                           "usage": {"total_tokens": 10}}).encode()
        result = smoke.response_summary(body, "application/json")
        self.assertEqual("tool_calls", result["finish_reason"])
        self.assertEqual(10, result["usage"]["total_tokens"])

    def test_truncated_stream_fails(self):
        with self.assertRaisesRegex(ValueError, "without data"):
            smoke.response_summary(b'data: {"choices": []}\n\n', "text/event-stream")

    def test_stream_error_is_not_a_success(self):
        with self.assertRaisesRegex(ValueError, "error event"):
            smoke.response_summary(b'data: {"error": {"type": "engine_error"}}\n\n'
                                   b'data: [DONE]\n\n', "text/event-stream")

    def test_accepts_v1_or_server_base(self):
        for suffix in ("", "/", "/v1", "/v1/"):
            self.assertEqual("http://localhost:8100", smoke.proxy_base("http://localhost:8100" + suffix))

    def test_one_failed_case_causes_nonzero_exit(self):
        with patch.object(sys, "argv", ["smoke.py", "--n", "1"]), \
                patch.object(smoke, "fetch", return_value=b""), \
                patch.object(smoke, "chat", side_effect=[{}, ValueError("bad stream"), {}, {}]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(1, smoke.main())


class NotificationTest(unittest.TestCase):
    def setUp(self):
        notify._seen.clear()

    def test_duplicate_delivery_is_suppressed_but_state_transitions_notify(self):
        alert = deepcopy(ALERT)
        with patch.object(notify, "notify", return_value=True) as deliver:
            self.assertEqual(1, notify.handle_alerts({"alerts": [alert]}))
            self.assertEqual(0, notify.handle_alerts({"alerts": [alert]}))
            alert["status"] = "resolved"
            self.assertEqual(1, notify.handle_alerts({"alerts": [alert]}))
            alert["status"] = "firing"
            self.assertEqual(1, notify.handle_alerts({"alerts": [alert]}))
            self.assertEqual(3, deliver.call_count)

    def test_instances_are_not_deduped_together(self):
        other = deepcopy(ALERT)
        other["labels"]["instance"] = "other:8101"
        with patch.object(notify, "notify", return_value=True):
            self.assertEqual(2, notify.handle_alerts({"alerts": [ALERT, other]}))

    def test_noncritical_alerts_do_not_notify(self):
        other = deepcopy(ALERT)
        other["labels"]["severity"] = "low"
        with patch.object(notify, "notify") as deliver:
            self.assertEqual(0, notify.handle_alerts({"alerts": [other]}))
            deliver.assert_not_called()

    def test_failed_notification_can_be_retried(self):
        with patch.object(notify, "notify", side_effect=[False, True]) as deliver:
            with self.assertRaises(notify.NotificationError):
                notify.handle_alerts({"alerts": [ALERT]})
            self.assertEqual(1, notify.handle_alerts({"alerts": [ALERT]}))
            self.assertEqual(2, deliver.call_count)

    def test_dedupe_expires(self):
        with patch.object(notify, "notify", return_value=True), \
                patch.object(notify.time, "monotonic", return_value=0):
            notify.handle_alerts({"alerts": [ALERT]})
        with patch.object(notify, "notify", return_value=True), \
                patch.object(notify.time, "monotonic", return_value=notify.DEDUPE_SECONDS + 1):
            self.assertEqual(1, notify.handle_alerts({"alerts": [ALERT]}))

    def test_concurrent_batches_notify_once(self):
        results = []
        with patch.object(notify, "notify", return_value=True) as deliver:
            threads = [threading.Thread(target=lambda: results.append(
                notify.handle_alerts({"alerts": [ALERT]}))) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(1, sum(results))
            deliver.assert_called_once()

    def test_osascript_failure_is_not_reported_as_success(self):
        result = subprocess.CompletedProcess([], 1, stderr=b"not allowed")
        with patch.object(notify.subprocess, "run", return_value=result), self.assertLogs(level="ERROR"):
            self.assertFalse(notify.notify("title", "message"))

    def test_alert_text_is_not_apple_script_source(self):
        text = 'bad"; do shell script "echo injected"'
        with patch.object(notify.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(notify.notify("title", text))
            command = run.call_args.args[0]
            self.assertNotIn(text, command[2])
            self.assertEqual(text, command[-1])

    def test_invalid_payloads_fail_validation(self):
        for payload in ([], {}, {"alerts": [None]}, {"alerts": [{"status": "unknown"}]},
                        {"alerts": [{"status": "firing", "labels": {"bad": 3}}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                notify.handle_alerts(payload)


class NotificationHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), notify.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def post(self, payload, path="/alert"):
        request = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_delivery_failure_returns_retryable_status(self):
        notify._seen.clear()
        with patch.object(notify, "notify", return_value=False):
            self.assertEqual(503, self.post({"alerts": [ALERT]})[0])

    def test_invalid_payload_returns_400(self):
        self.assertEqual(400, self.post([])[0])

    def test_unknown_routes_cannot_trigger_notification(self):
        with patch.object(notify, "notify") as deliver:
            self.assertEqual(404, self.post({}, "/test")[0])
            deliver.assert_not_called()


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.verifier = verify.Verifier()

    def test_down_target_cannot_pass(self):
        data = {"activeTargets": [{"labels": {"job": job}, "health": "down" if job == "loki" else "up"}
                                 for job in ("splash-proxy", "splash-runtime", "prometheus-self", "loki")]}
        with patch.object(self.verifier, "prometheus_data", return_value=data):
            with self.assertRaisesRegex(ValueError, "loki"):
                self.verifier.targets()

    def test_missing_alertmanager_destination_cannot_pass(self):
        with patch.object(self.verifier, "prometheus_data", return_value={"activeAlertmanagers": []}):
            with self.assertRaisesRegex(ValueError, "no alertmanager"):
                self.verifier.alertmanager()

    def test_redacted_webhook_url_is_not_a_missing_receiver(self):
        with patch.object(self.verifier, "prometheus_data", return_value={
                "activeAlertmanagers": [{"url": "http://alertmanager:9093/api/v2/alerts"}]}), \
                patch.object(self.verifier, "get", side_effect=[
                    {"config": {"original": "webhook_configs:\n  url: <secret>"}},
                    [{"name": "desktop"}, {"name": "quiet"}]]):
            self.assertIn("receiver loaded", self.verifier.alertmanager())

    def test_nan_query_cannot_pass(self):
        with patch.object(self.verifier, "prometheus_data",
                          return_value={"result": [{"value": [1, "NaN"]}]}):
            with self.assertRaises(ValueError):
                self.verifier.query("splash_info")

    def test_wrong_grafana_datasource_uid_cannot_pass(self):
        with patch.object(self.verifier, "get", return_value={"uid": "generated-id", "type": "prometheus"}):
            with self.assertRaisesRegex(ValueError, "datasource UID"):
                self.verifier.dashboards()

    def test_empty_loki_is_only_ok_without_trace_test(self):
        with patch.object(self.verifier, "get", return_value={"status": "success", "data": {"result": []}}):
            self.assertIn("no traffic is OK", self.verifier.traces())
            with self.assertRaisesRegex(ValueError, "expected 4"):
                self.verifier.traces(start_ns=1)

    def test_failure_does_not_print_all_checks_passed(self):
        def fail():
            raise ValueError("missing data")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(verify.run_checks([("broken", fail)], wait=0))
        self.assertIn("FAIL: missing data", output.getvalue())


class DashboardTest(unittest.TestCase):
    def test_new_panel_has_units_estimate_label_and_no_overlaps(self):
        path = SCRIPTS.parent / "grafana/dashboards/splash-requests.json"
        panels = json.loads(path.read_text())["panels"]
        panel = next(p for p in panels if p["id"] == 13)
        self.assertIn("estimate", panel["title"])
        self.assertEqual("ms", panel["fieldConfig"]["defaults"]["unit"])
        for index, first in enumerate(panels):
            for second in panels[index + 1:]:
                a, b = first["gridPos"], second["gridPos"]
                overlap = (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
                           a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
                self.assertFalse(overlap, (first["title"], second["title"]))
        for panel in panels:
            if panel["datasource"]["type"] == "loki":
                self.assertTrue(panel["targets"][0]["expr"])


if __name__ == "__main__":
    unittest.main()
