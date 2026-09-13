#!/usr/bin/env bash
# Run Gemini Code from any directory: the agent works in the directory you call it from.
set -euo pipefail
SCRIPT="$(readlink -f "$0" 2>/dev/null || echo "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
VENV="${GCODE_VENV:-$HOME/.venvs/gemini-code}"

if [ ! -x "$VENV/bin/python" ]; then
  echo "ยังไม่ได้ติดตั้ง ให้รัน: \"$APP_DIR/setup.sh\"" >&2
  exit 1
fi

PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$VENV/bin/python" -m gemini_code "$@"
