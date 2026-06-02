#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"

export FENCING_SERIAL_PORT="${FENCING_SERIAL_PORT:-/dev/ttyACM0}"
export FENCING_CAMERA_INDEX="${FENCING_CAMERA_INDEX:-0}"
export PYTHONNOUSERSITE=1

cd "$SCRIPT_DIR"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" control_fencing.py
