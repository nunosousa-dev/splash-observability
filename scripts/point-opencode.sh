#!/usr/bin/env bash
# Point OpenCode (or any OpenAI-compatible client config) at the observability proxy.
#
#   ./scripts/point-opencode.sh            # route the 'splash' provider through the proxy
#   ./scripts/point-opencode.sh --restore  # go back to Splash directly
set -euo pipefail

CONFIG="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
PROXY="${SPLASH_OBS_PROXY:-http://127.0.0.1:8100/v1}"
DIRECT="${SPLASH_DIRECT:-http://127.0.0.1:8000/v1}"

if [ ! -f "$CONFIG" ]; then
  echo "config not found: $CONFIG"
  exit 1
fi

BACKUP="${CONFIG}.bak"
if [ ! -f "$BACKUP" ]; then
  cp "$CONFIG" "$BACKUP"
  echo "backup written to $BACKUP"
fi

TARGET="$PROXY"
if [ "${1:-}" = "--restore" ]; then
  TARGET="$DIRECT"
fi

python3 - "$CONFIG" "$TARGET" <<'PY'
import json, sys

path, target = sys.argv[1], sys.argv[2]
with open(path) as fh:
    config = json.load(fh)

providers = config.get("providers", {})
if "splash" not in providers:
    print("no 'splash' provider found in %s" % path)
    sys.exit(1)

providers["splash"].setdefault("settings", {})["baseURL"] = target
with open(path, "w") as fh:
    json.dump(config, fh, indent=2)
print("providers.splash.settings.baseURL -> %s" % target)
PY

echo "Done. Restart OpenCode for the change to take effect."
