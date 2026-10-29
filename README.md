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

## Work tracking

Remaining and future work lives in the private
**[Splash Observability Kanban board](https://github.com/users/nunosousa-dev/projects/4/views/1)**,
linked to this repository. GitHub sign-in with access to the project is required.

**Backlog → Ready → In progress → In review → Done**

Create a [repository issue](https://github.com/nunosousa-dev/splash-observability/issues)
with the problem/outcome, scope, acceptance criteria, and validation steps. Select
**Splash Observability** in the issue's **Projects** sidebar, start it in **Backlog**,
and set **Priority** to High, Medium, or Low. Keep the corresponding `priority:*` label
consistent with the project field. Move actionable work to Ready, use In progress only
when implementation starts, and move reviewed/validated work to Done and close the
issue. Adding a normal repository issue does **not** automatically add it to the board.

## Start and stop: what runs where

Run the commands below from the root of this repository. You need Docker Desktop
running with the Docker Compose plugin, Python 3.9+, `make`, and `curl`.
The host desktop notifier requires macOS.

There are **three independently managed parts**; no single command currently starts
or stops all three:

| Part | Runs in | Start | Stop |
| --- | --- | --- | --- |
| Splash inference server | Your Mac, outside this project | Start its server/model using your normal Splash app or CLI setup | Stop it in that app, or Ctrl-C in its serving terminal |
| Proxy, Prometheus, Grafana, Loki, Alertmanager | Five Docker Compose containers | `make up` | `docker compose down` |
| Desktop notification listener (optional) | A separate terminal on your Mac | `make notify` | Ctrl-C in that terminal |

### Start a normal session

**1. Start Docker Desktop and Splash.** On macOS, you can open Docker Desktop with:

```bash
open -a Docker
docker info
```

Wait for `docker info` to succeed. This project does not install Splash, choose its
model, or start its inference server. If you use the Splash app, start its server
there. For the CLI, run `splash serve` in its **own terminal** and leave it open.
For example, using the model ID configured as this project's smoke-test default:

```bash
splash serve \
  --model 'ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0' \
  --host 127.0.0.1 \
  --port 8000
```

Use a supported model that fits your Mac; the first run may download model files.
Do not launch a second server if the app or another terminal already owns port 8000.
Wait for Splash to report **Ready**. Pressing Ctrl-C in this terminal later stops
inference. See `splash serve --help` for model/cache options.

From another terminal, check that the API is listening:

```bash
curl -fsS --max-time 10 http://127.0.0.1:8000/health
curl -fsS --max-time 10 http://127.0.0.1:8000/v1/models
```

If either command fails, get Splash running before testing the full stack. Starting
the containers alone does not make an unavailable inference server healthy.

**2. Start the Docker stack in terminal A:**

```bash
make up
```

This builds the proxy image, creates/starts all five containers in the background,
waits for the configured container healthchecks, and prints their URLs and scrape
target status. You can close terminal A afterward; the containers keep running.

**3. For desktop alerts, open terminal B in this repository and leave this running:**

```bash
make notify
```

This command intentionally stays in the foreground. Keep this terminal open for
notifications. It is not started by `make up` and is not installed as a login service.
Skip it if you only want dashboards and the Alertmanager web UI. If port 9393 is
already occupied by this listener, do not start a second copy; see troubleshooting
below.

**4. Back in terminal A, verify the stack:**

```bash
make verify                               # health/data checks; no inference generated
make verify TRACES=1                      # also sends four live inference requests
./scripts/verify.sh --traces --notify      # also requires the desktop listener
```

The live checks require a loaded model. If the smoke test's default model does not
match yours, set the exact model ID returned by `/v1/models`:

```bash
SPLASH_MODEL='your-model-id' make verify TRACES=1
```

**5. Optionally route OpenCode through the proxy:**

```bash
make route-opencode
```

This updates the existing `splash` provider's base URL in
`~/.config/opencode/opencode.json` (or `OPENCODE_CONFIG`) and creates a `.bak` backup
if none exists. **Restart OpenCode** for the setting to take effect. For other
OpenAI-compatible clients, use `http://127.0.0.1:8100/v1` instead of Splash's direct
`http://127.0.0.1:8000/v1` URL.

### Stop a normal session without deleting data

**1. Switch clients away from the proxy before stopping it.** If you routed OpenCode
and want to keep using Splash directly:

```bash
make unroute-opencode
```

Restart OpenCode before continuing inference. This command sets the provider URL
back to `http://127.0.0.1:8000/v1`; it does not restore the whole `.bak` configuration.
Update other clients' base URLs too, or stop those clients. Otherwise, they will keep
trying the stopped proxy on port 8100.

**2. In terminal B, press Ctrl-C** to stop the desktop notifier.

**3. In terminal A, stop and remove the five containers and their Compose network:**

```bash
docker compose down
```

This preserves the named volumes containing Prometheus metrics, Grafana settings,
and Loki logs. It also leaves `logs/requests.jsonl` and its rotated files on the host.
Running `make up` later reuses that data. **Do not add `--volumes` / `-v`** unless
you intentionally want to delete the stack's persisted container data.

**4. Stop Splash separately** in its app or serving terminal if you no longer need
inference. You can leave Splash running if another client still uses it directly.
Stopping Alertmanager before Splash avoids a needless "Splash unreachable" desktop
alert during an intentional shutdown.

> **About `make down`:** the current wrapper calls `docker compose down`, but also runs
> `pkill -f splash_obs_proxy.py`, which can stop host proxies belonging to other
> checkouts or tests. It does not stop Splash or the desktop notifier. Prefer the
> explicit `docker compose down` command above until
> [issue #8](https://github.com/nunosousa-dev/splash-observability/issues/8) is fixed.

### Restart, inspect, or stop individual services

```bash
docker compose ps                         # container state and health
docker compose logs --tail 100            # recent logs from all containers
docker compose logs --tail 100 -f proxy   # follow one service; Ctrl-C stops only the log viewer

docker compose restart                   # restart all existing stack containers
docker compose restart prometheus        # restart one existing service after a config edit
docker compose stop grafana              # stop one service, leaving its container/data intact
docker compose start grafana             # start that existing container again

make up                                  # rebuild proxy/apply Compose changes; also resumes stopped services
make targets                             # Prometheus scrape health
make alerts                              # alerts currently visible in Alertmanager
make report                              # summary of the local trace log
make test                                # offline tests; no running services or model required
```

Valid Compose service names are `proxy`, `prometheus`, `grafana`, `loki`, and
`alertmanager`. `docker compose restart` does not rebuild images or apply changed
Compose environment settings: use `make up` for those. For an edited bind-mounted
Prometheus, Loki, Alertmanager, or Grafana provisioning file, restart the affected
service so it reloads the file. The host notifier must be stopped with Ctrl-C and
started again separately to pick up changes to its script or environment.

### Service URLs

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | <http://localhost:3000> | anonymous viewer; dashboards in the **Splash** folder |
| Prometheus | <http://localhost:9090> | 4 scrape targets, all should be `up` |
| Alertmanager | <http://localhost:9093> | critical alerts go to the host desktop listener |
| Loki | <http://localhost:3100> | trace logs, `{app="splash-obs"}` |
| Proxy (client) | `http://127.0.0.1:8100/v1` | point OpenAI-compatible clients here |
| Proxy (metrics) | <http://127.0.0.1:8101/metrics> | loopback only, for curling locally |
| Desktop notifier | <http://127.0.0.1:9393/healthz> | only available while `make notify` is running |
| Splash (external) | <http://127.0.0.1:8000/health> | start/stop separately using your Splash setup |

### Common start/stop problems

- **Cannot connect to Docker:** start Docker Desktop and wait for `docker info` to
  succeed, then run `make up` again.
- **Proxy returns 502 / `splash-runtime` is down:** check Splash's two API URLs above.
  The proxy container's own healthcheck can pass while Splash is unavailable.
- **Startup fails a healthcheck:** inspect `docker compose ps` and
  `docker compose logs --tail 100 <service>`. Do not delete volumes as a first fix.
- **Notifier says "Address already in use":** check the existing listener with
  `curl -fsS --max-time 5 http://127.0.0.1:9393/healthz`. If you need to stop a copy whose
  terminal is gone, run `lsof -nP -iTCP:9393 -sTCP:LISTEN`, inspect its PID using
  `ps -p PID -o pid=,command=`, and only after confirming it is this project's
  `notify-macos.py`, run `kill -TERM PID` (replace `PID` with the verified number).
  Do not use a broad `pkill python` or kill an unknown process to free the port.
- **OpenCode fails after the stack stops:** its base URL may still point to 8100.
  Run `make unroute-opencode` and restart OpenCode, or bring the proxy back with
  `make up`.

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
Run the host listener in terminal B, as described in the start sequence. In terminal A
you can test notifications and check its health:

```bash
make notify-test             # ask macOS to show a test notification
./scripts/verify.sh --notify # also check that the host listener is reachable
```

The listener is intentionally outside Docker so `osascript` can reach your desktop.
It binds to loopback by default. Allow notifications for the relevant application in
macOS System Settings and check Focus/Do Not Disturb if nothing appears. A successful
`osascript` call means the OS accepted the request, not that a banner was visible.
Neither `make up` nor `make down` starts/stops this listener or installs a login
service. Without it, Alertmanager retries webhook deliveries. Optional login-time
startup is tracked in [issue #7](https://github.com/nunosousa-dev/splash-observability/issues/7).

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

This is an alternative to the containerized proxy, not a second proxy to run alongside
it on the same ports. Press Ctrl-C in its terminal to stop it. Do not run `make proxy`
while Compose's `proxy` is already using ports 8100/8101. The default host invocation
does not push to Loki unless `LOKI_URL` is set; the normal Compose setup configures it.

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
