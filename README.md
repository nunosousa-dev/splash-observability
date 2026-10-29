# Splash observability

A local monitoring stack for [Splash](https://github.com/incoai/splash), an Apple-Silicon
LLM inference server. It puts a tracing proxy in front of Splash and pipes everything into
Prometheus, Grafana, Loki and Alertmanager.

The proxy is **metadata-only**: it records model, latency, token counts, tool calls and
errors, and never stores prompt or completion text. That is what makes it acceptable to
run in front of a coding agent all day.

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
make route-opencode      # point OpenCode's splash provider at the proxy
make test                # proxy unit tests (stub upstream, no model needed)
make verify TRACES=1     # end-to-end check of the whole stack
```

Then browse:

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | <http://localhost:3000> | anonymous viewer; dashboards in the **Splash** folder |
| Prometheus | <http://localhost:9090> | 4 scrape targets, all should be `up` |
| Alertmanager | <http://localhost:9093> | needs a receiver configured to notify |
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

**Splash request traces** (12 panels) reads the proxy's request-level metrics plus Loki
for per-request detail: request rate, error rate, client aborts, tokens in/out, tool
calls, duration and TTFT. This is how your clients are actually using the engine.

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
way through. That keeps Splash loopback-only with no changes on its side — but it also
means a request sent to the proxy from inside the container with the real Host header
gets `403 Forbidden`. Verify passthrough through the proxy, not around it.

### Streaming

Splash ends an SSE stream with `data: [DONE]` and then keeps the connection alive for
reuse. The proxy stops reading at that sentinel; waiting for EOF instead would add the
full upstream timeout (up to 600s) to every streamed request and badly inflate the
recorded duration. This is covered by `test_stream_completes_without_waiting_for_eof`.

`ttft_seconds` is recorded for streaming requests only: a non-stream response has no
first token separate from its whole body, so including it would push total duration into
the same p95. Per-request `ttft_ms` is still in the trace log, filterable by `stream`.

## Prometheus and Grafana versions matter here

Image tags are pinned in `docker-compose.yml`, deliberately. Several config keys in this
repo are version-sensitive, and `latest` silently crossing a major version is what broke
the stack originally:

- **Prometheus 3.x** rejects the `name:` / `targets:` scrape-config shorthand that 2.x
  accepted, and requires `job_name:` plus `static_configs:`. It also fails the whole
  config load (crash loop) rather than warning.
- **Grafana 13** requires a top-level `providers:` key and the directory under
  `options.path`; the older `filename:` key panics the provisioning stub at startup.
- **Loki 3.x** accepts the pre-1.x flat push body (`{timestamp, line, labels}`) with an
  HTTP 200 and stores nothing. It must be `{"stream": {…}, "values": [[ts, line]]}`.

Bump versions on purpose and run `make verify TRACES=1` afterwards.

## Failure modes worth knowing

The failure style in this stack is *silent*: a listener that binds but never serves, an
exposition line that truncates the rest of the scrape, a push that returns 200 and drops
the data, a histogram that reports NaN until it has two samples. `scripts/verify.sh`
exists to check each of those in one pass, and the unit tests assert the invariants
directly. If something looks unexpectedly empty, start there.

## Layout

```
proxy/          the trace proxy + Dockerfile
prometheus/     scrape config + alert and recording rules
grafana/        provisioned datasources and dashboards
loki/           single-binary Loki, filesystem storage
alertmanager/   routing (no receiver configured by default)
scripts/        up/down, proxy, smoke, report, verify, point-opencode
tests/          stdlib-only proxy tests against a stub upstream
logs/           trace log output (gitignored)
docs/           engine-selection research notes
```

## Make targets

`make up` · `down` · `proxy` · `smoke` · `test` · `verify` · `report` · `targets` ·
`alerts` · `route-opencode` · `unroute-opencode`
