#!/usr/bin/env bash
# Interpreter preference:
#   1. .venv          — pyenv 3.14 + the anthropic SDK (full sit/start AI layer)
#   2. python3 on PATH — whatever pyenv/Homebrew resolves to
#   3. /usr/bin/python3 — Apple's system Python; the app is stdlib-only, so a
#      fresh clone with no setup at all still runs, minus the SDK path.
set -euo pipefail
cd "$(dirname "$0")"
if   [ -x .venv/bin/python ];   then PY=.venv/bin/python
elif command -v python3 >/dev/null; then PY="$(command -v python3)"
else PY=/usr/bin/python3
fi
echo "using $PY ($("$PY" --version 2>&1))" >&2
exec "$PY" quick/draftboard.py "$@"
