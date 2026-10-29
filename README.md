# Splash observability

A local monitoring stack for [Splash](https://github.com/incoai/splash), an Apple-Silicon
LLM inference server. It puts a tracing proxy in front of Splash and pipes everything into
Prometheus, Grafana, Loki and Alertmanager.

The proxy is **metadata-only**: it records model, latency, token counts, tool calls and
errors, without intentionally recording prompt or completion text. Trace metadata
(model names, tool names, client addresses and request sizes) can still be sensitive;
the log directory is excluded from Git.

```
OpenCode / scripts ──▶ proxy :8100 ──▶ Splash :8000  (loopback, unchanged)
                          │
                          ├─▶ :8101 /metrics          ──▶ Prometheus ──▶ Grafana
                          ├─▶ :8101 /metrics/splash   ──────────────────▶   :3000
                          ├─▶ logs/requests.jsonl  (rotated, local)
                          └─▶ Loki :3100              ──▶ Grafana
Prometheus ──▶ Alertmanager :9093
```

## Quick start

Splash must already be running on `127.0.0.1:8000`.

```bash
make up                  # compose up -d + build
make notify              # optional: desktop alert listener (leave in a separate terminal)
make route-opencode      # point OpenCode's splash provider at the proxy
make test                # proxy unit tests (stub upstream, no model needed)
make verify TRACES=1     # end-to-end check of the whole stack
```

Then browse:

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | <http://localhost:3000> | anonymous viewer; dashboards in the **Splash** folder |
| Prometheus | <http://localhost:9090> | 4 scrape targets, all should be `up` |
| Alertmanager | <http://localhost:9093> | critical alerts go to the host desktop listener |
| Loki | <http://localhost:3100> | trace logs, `{app="splash-obs"}` |
| Proxy (client) | `http://127.0.0.1:8100/v1` | point OpenAI-compatible clients here |
| Proxy (metrics) | <http://127.0.0.1:8101/metrics> | loopback only, for curling locally |

`make down` stops everything and keeps the volumes. `make unroute-opencode` puts
OpenCode back to talking to Splash directly.

## The two dashboards

**Splash runtime — inference engine** (16 panels) reads Splash's own `/metrics` via the
proxy passthrough: scheduler state, KV cache pages, prefill/decode throughput, prompt
cache reuse, speculative-draft acceptance, memory and thermal pressure, latency
distributions. This is the engine's health.

**Splash request traces** (13 panels) reads the proxy's request-level metrics plus Loki
for per-request detail: request rate, error rate, client aborts, tokens in/out, tool
calls, duration, streaming TTFT and a stream chunk-gap estimate. The failed-request log
panel filters HTTP 4xx/5xx responses; the all-traces panel includes every request.

## Desktop alerts

Prometheus sends firing alerts to `alertmanager:9093`. Alertmanager forwards **critical**
alerts and their resolutions to `http://host.docker.internal:9393/alert`;
non-critical alerts stay visible in the Alertmanager UI without desktop notifications.
Run the host listener in a separate terminal:

```bash
make notify                  # foreground; Ctrl-C stops only the listener
make notify-test             # ask macOS to show a test notification
./scripts/verify.sh --notify # also check that the host listener is reachable
```

The listener is intentionally outside Docker so `osascript` can reach your desktop.
It binds to loopback by default. Allow notifications for the relevant application in
macOS System Settings and check Focus/Do Not Disturb if nothing appears. A successful
`osascript` call means the OS accepted the request, not that a banner was visible.
`make up`/`make down` manage containers only; they do not install a login service or
start/stop this host listener. Without it, Alertmanager retries webhook deliveries.

`SPLASH_NOTIFY_HOST`, `SPLASH_NOTIFY_PORT` and `SPLASH_NOTIFY_DEDUPE` configure the listener
(defaults: `127.0.0.1`, `9393`, `600` seconds). If you change the address, update the URL
in `alertmanager/alertmanager.yml` too. Duplicate deliveries are suppressed per alert
label set and state, while resolved/re-firing transitions notify immediately. A failed
`osascript` invocation returns HTTP 503 so Alertmanager can retry.

To use Slack/email instead, configure that receiver in `alertmanager/alertmanager.yml`
and keep credentials outside version control. The supplied route is local-only and
needs no external notification service.

## Proxy

`proxy/splash_obs_proxy.py` is a single stdlib-only file (Python 3.9+, no dependencies)
so it is easy to read, audit and run outside Docker:

```bash
make proxy               # foreground, reads SPLASH_* env vars
```

Counters, gauges and histograms are rendered by hand into the Prometheus text format.
All metric mutations take one lock, since `ThreadingHTTPServer` handles each connection
on its own thread.

Traces go to `logs/requests.jsonl` (size-rotated: `SPLASH_OBS_TRACE_MAX_BYTES`, default
64 MiB, `0` disables; `SPLASH_OBS_TRACE_KEEP` files in total) and to Loki when
`LOKI_URL` is set.

### Loopback and Host headers

Splash listens on `127.0.0.1` only and allow-lists the `Host` header. The proxy container
reaches the Mac through `host.docker.internal`, so it rewrites `Host: 127.0.0.1` on the
way through. That keeps Splash loopback-only with no changes on its side. A direct
container-to-Splash probe using `Host: host.docker.internal` can get `403 Forbidden`;
the proxy supplies the allowed header. Probe `/metrics/splash` through the proxy.

### Streaming

Splash ends an SSE stream with `data: [DONE]` and then keeps the connection alive for
reuse. The proxy stops reading at that sentinel; waiting for EOF instead would add the
full upstream timeout (up to 600s) to every streamed request and badly inflate the
recorded duration. This is covered by `test_stream_completes_without_waiting_for_eof`.

`ttft_seconds` is recorded for streaming requests only: a non-stream response has no
first token separate from its whole body, so including it would push total duration into
the same p95. Per-request `ttft_ms` is still in the trace log, filterable by `stream`.

`splash_obs_itl_seconds` / `itl_ms` currently measure each request's **mean gap between
stream reads**, not per-token ITL. Several tokens or metadata events may share a read.
The dashboard labels it as an estimate; use Splash's own `splash_itl_*` metrics for
engine-level inter-token latency. The two measurements should not be equated.

`make smoke` parses both plain JSON and SSE, including the final usage frame whose
`choices` array can be empty. It reports streaming token usage and finish reason,
and exits nonzero if any case fails or a stream is truncated.

## Prometheus and Grafana versions matter here

Image tags are pinned in `docker-compose.yml`. Several stack failures came from invalid
configuration, not necessarily a change between major versions:

- **Prometheus** scrape jobs use `job_name` plus `static_configs`. Bare `name`/`targets`
  are invalid keys, not a supported Prometheus 2.x shorthand.
- **Grafana** uses `apiVersion: 1`, `providers` and `options.path` for file provisioning.
  The incorrect `filename` setting caused a startup panic in the tested image.
  Datasource provisioning also needs `apiVersion: 1`; without it, configured UIDs were
  ignored and the provisioned dashboards could not find Prometheus or Loki.
- **Loki's JSON push API** expects `{"streams": [{"stream": {…}, "values": [[ts, line]]}]}`.
  The old code sent an invalid flat object and no traces appeared, despite HTTP success.

Bump versions on purpose and run `make verify TRACES=1` afterwards.

If upgrading a stack created with the old datasource file (no `apiVersion`), the
stored UIDs may be generated values instead of `prometheus`/`loki`. Changing the YAML
alone can cause a "data source not found" provisioning error. Use a one-time
`apiVersion: 1` provisioning file with `deleteDatasources` entries for **Prometheus**
and **Loki** in org 1, restart Grafana so the canonical file recreates those two
config-managed records, then remove the migration file. Do not delete the Grafana
volume; dashboards, users, and all metric/log volumes can be preserved.

## Failure modes worth knowing

The failure style in this stack is *silent*: a listener that binds but never serves, an
exposition line that truncates the rest of the scrape, a push that returns 200 and drops
the data, a histogram that reports NaN until it has two samples. `scripts/verify.sh`
exists to check each of those in one pass, and the unit tests assert the invariants
directly. If something looks unexpectedly empty, start there.

`make verify` asserts four healthy scrape jobs, evaluated rules, Alertmanager discovery,
datasource UIDs, both provisioned dashboards (including the chunk-gap panel), and Loki query readiness.
`make verify TRACES=1` additionally generates four requests and requires fresh Loki
entries and scraped request/latency samples. It retries readiness checks for up to 60
seconds so a scrape/provisioning delay is not mistaken for data loss. No traffic is
required for the ordinary check; an empty log is only a failure with `--traces`.

The Docker ports for Grafana, Prometheus, Loki and Alertmanager currently bind to all
interfaces, unlike the loopback-bound proxy and notifier. This is a trusted local
development setup, **not an internet-facing deployment**. Bind those ports to loopback
or add authentication/firewall rules before using an untrusted network.

## Layout

```
proxy/          the trace proxy + Dockerfile
prometheus/     scrape config + alert and recording rules
grafana/        provisioned datasources and dashboards
loki/           single-binary Loki, filesystem storage
alertmanager/   critical-only desktop webhook routing
scripts/        up/down, proxy, smoke, report, verify, notifier, point-opencode
tests/          stdlib-only proxy and tooling tests; no model required
logs/           trace log output (gitignored)
docs/           engine-selection research notes
```

## Make targets

`make up` · `down` · `proxy` · `smoke` · `test` · `verify` · `report` · `targets` ·
`alerts` · `notify` · `notify-test` · `route-opencode` · `unroute-opencode`
