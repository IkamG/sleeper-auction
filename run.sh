#!/usr/bin/env bash
# Prefer the venv (Python 3.14 + anthropic SDK) when it exists; otherwise fall
# back to the system Python. The app is fully functional either way — the venv
# only upgrades the sit/start AI layer from raw HTTP to the official SDK.
set -euo pipefail
cd "$(dirname "$0")"
PY=/usr/bin/python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" quick/draftboard.py "$@"
