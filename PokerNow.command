#!/bin/bash
# Double-click launcher for the PokerNow hand-history tool (macOS).
# - creates the virtualenv on first run (uv if available, else python3 -m venv)
# - keeps all game data in ./data/<gameId>/ next to this file
# - reads ./.env for optional settings (e.g. POKERNOW_NPT=... to include your hole cards)
# - starts the web UI and opens it in your browser; close this window (or Ctrl-C) to stop

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT="${POKERNOW_PORT:-8765}"
export POKERNOW_DATA_DIR="${POKERNOW_DATA_DIR:-$DIR/data}"
mkdir -p "$POKERNOW_DATA_DIR"

if [ -f "$DIR/.env" ]; then
  set -a; . "$DIR/.env"; set +a
fi

echo "PokerNow hand-history tool"
echo "  folder : $DIR"
echo "  data   : $POKERNOW_DATA_DIR"

if [ ! -x "$DIR/.venv/bin/pokernow" ]; then
  echo "First run: setting up the Python environment…"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q "$DIR/.venv"
    uv pip install -q --python "$DIR/.venv/bin/python" -e "$DIR"
  else
    PY="$(command -v python3 || true)"
    if [ -z "$PY" ]; then
      echo "python3 not found. Install Python 3.11+ (https://www.python.org/downloads/) and double-click again."
      read -r -p "Press Enter to close…" _; exit 1
    fi
    "$PY" -m venv "$DIR/.venv"
    "$DIR/.venv/bin/pip" install -q --upgrade pip
    "$DIR/.venv/bin/pip" install -q -e "$DIR"
  fi
fi

# pick a free port if the default is busy
while lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done

echo "  url    : http://127.0.0.1:$PORT/   (closing this window stops the server)"
if [ -n "$POKERNOW_NPT" ]; then echo "  login  : POKERNOW_NPT set — your own hole cards will be included"; else echo "  login  : anonymous (add POKERNOW_NPT=... to .env to include your hole cards)"; fi
echo

exec "$DIR/.venv/bin/pokernow" serve --host 127.0.0.1 --port "$PORT" --open
