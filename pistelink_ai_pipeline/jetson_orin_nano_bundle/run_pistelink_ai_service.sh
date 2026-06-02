#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"

export PISTELINK_AI_SOCKET="${PISTELINK_AI_SOCKET:-/run/pistelink/ai.sock}"
export PISTELINK_MATCH_ROOT="${PISTELINK_MATCH_ROOT:-/var/lib/pistelink/matches}"
export PISTELINK_ANALYZER_ROOT="${PISTELINK_ANALYZER_ROOT:-${SCRIPT_DIR}/../portable_fencing_pipeline_low_latency_streaming}"
export PISTELINK_ANALYZER_FISHEYE_BACKEND="${PISTELINK_ANALYZER_FISHEYE_BACKEND:-none}"
export PYTHONNOUSERSITE=1

cd "$SCRIPT_DIR"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" "${SCRIPT_DIR}/pistelink_ai_service.py" "$@"
