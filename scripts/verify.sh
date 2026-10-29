#!/usr/bin/env bash
# End-to-end stack assertions; --traces also exercises live inference.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python3}" "$HERE/verify.py" "$@"
