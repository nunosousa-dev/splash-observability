DOCKER ?= $(shell command -v docker || echo /opt/homebrew/bin/docker)

.PHONY: up down proxy smoke test verify report targets alerts notify notify-test route-opencode unroute-opencode

up:            ## start containers + proxy
	./scripts/up.sh

down:          ## stop everything
	./scripts/down.sh

proxy:         ## run the trace proxy in the foreground
	./scripts/run-proxy.sh

smoke:         ## send test requests through the proxy and print what was captured
	./scripts/smoke.py

test:          ## proxy unit tests (stub upstream, no model needed)
	python3 -W error::ResourceWarning -m unittest discover -s tests -v

verify:        ## end-to-end check of the whole stack (TRACES=1 also runs a smoke test)
	./scripts/verify.sh $(if $(TRACES),--traces,)

report:        ## local summary of the trace log
	./scripts/report.py

targets:       ## show Prometheus scrape target health
	curl -s http://localhost:9090/api/v1/targets | python3 -c 'import sys,json; [print(t["labels"].get("job"), t["health"], t.get("lastScrape")) for t in json.load(sys.stdin)["data"]["activeTargets"]]'

alerts:        ## list firing alerts
	curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool

notify:        ## run the critical-alert desktop listener on this Mac (foreground)
	python3 scripts/notify-macos.py

notify-test:   ## ask macOS to display a test notification
	python3 scripts/notify-macos.py --test

route-opencode:## point OpenCode's splash provider at the proxy
	./scripts/point-opencode.sh

unroute-opencode:
	./scripts/point-opencode.sh --restore
