#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"

choose_runtime_dir() {
  if [[ -n "${PISTELINK_RUNTIME_DIR:-}" ]]; then
    mkdir -p "$PISTELINK_RUNTIME_DIR"
    printf '%s\n' "$PISTELINK_RUNTIME_DIR"
    return
  fi

  local user_part="${USER:-$(id -u)}"
  local candidates=(
    "/run/pistelink"
  )
  if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    candidates+=("${XDG_RUNTIME_DIR}/pistelink")
  fi
  candidates+=("/tmp/pistelink-${user_part}")

  local candidate
  for candidate in "${candidates[@]}"; do
    if mkdir -p "$candidate" 2>/dev/null && [[ -w "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  printf 'No writable PisteLink runtime directory found\n' >&2
  exit 1
}

choose_match_root() {
  if [[ -n "${PISTELINK_MATCH_ROOT:-}" ]]; then
    mkdir -p "$PISTELINK_MATCH_ROOT"
    printf '%s\n' "$PISTELINK_MATCH_ROOT"
    return
  fi

  if mkdir -p /var/lib/pistelink/matches 2>/dev/null && [[ -w /var/lib/pistelink/matches ]]; then
    printf '%s\n' "/var/lib/pistelink/matches"
    return
  fi

  if [[ -n "${HOME:-}" ]]; then
    local home_root="${HOME}/.local/share/pistelink/matches"
    mkdir -p "$home_root"
    printf '%s\n' "$home_root"
    return
  fi

  local fallback_root="${PISTELINK_RUNTIME_DIR}/matches"
  mkdir -p "$fallback_root"
  printf '%s\n' "$fallback_root"
}

choose_camera_device() {
  if [[ -n "${FENCING_CAMERA_DEVICE:-}" ]]; then
    printf '%s\n' "$FENCING_CAMERA_DEVICE"
    return
  fi

  local candidate
  for candidate in /dev/v4l/by-id/*-video-index0 /dev/v4l/by-path/*-video-index0; do
    if [[ -e "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
}

export PISTELINK_RUNTIME_DIR="$(choose_runtime_dir)"
export PISTELINK_AI_SOCKET="${PISTELINK_AI_SOCKET:-${PISTELINK_RUNTIME_DIR}/ai.sock}"
export PISTELINK_MATCH_ROOT="$(choose_match_root)"
export PISTELINK_ANALYZER_ROOT="${PISTELINK_ANALYZER_ROOT:-${SCRIPT_DIR}/../portable_fencing_pipeline_low_latency_streaming}"
export PISTELINK_ANALYZER_FISHEYE_BACKEND="${PISTELINK_ANALYZER_FISHEYE_BACKEND:-none}"
export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"

CAMERA_DEVICE="$(choose_camera_device || true)"
if [[ -n "$CAMERA_DEVICE" ]]; then
  export FENCING_CAMERA_DEVICE="$CAMERA_DEVICE"
fi
export FENCING_CAMERA_WIDTH="${FENCING_CAMERA_WIDTH:-1280}"
export FENCING_CAMERA_HEIGHT="${FENCING_CAMERA_HEIGHT:-720}"
export FENCING_CAMERA_FPS="${FENCING_CAMERA_FPS:-30}"

cd "$SCRIPT_DIR"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

printf '[PISTELINK] AI socket: %s\n' "$PISTELINK_AI_SOCKET" >&2
printf '[PISTELINK] AI match root: %s\n' "$PISTELINK_MATCH_ROOT" >&2
if [[ -n "${FENCING_CAMERA_DEVICE:-}" ]]; then
  printf '[PISTELINK] AI camera device: %s\n' "$FENCING_CAMERA_DEVICE" >&2
else
  printf '[PISTELINK] AI camera device: auto probe\n' >&2
fi

exec "$PYTHON_BIN" "${SCRIPT_DIR}/pistelink_ai_service.py" "$@"
