#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FENCING_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
AI_DIR="${FENCING_ROOT}/pistelink_ai_pipeline"
AI_BUNDLE_DIR="${AI_DIR}/jetson_orin_nano_bundle"

REMOTE_HOST=""
REMOTE_PATH="${PISTELINK_REMOTE_PATH:-/home/thomas/fencing/pistelink}"
RUN_INSTALL="${PISTELINK_RUN_INSTALL:-auto}"
WAIT_TIMEOUT="${PISTELINK_WAIT_TIMEOUT:-120}"
TUNNEL=0
LOCAL_TUNNEL_PORT="${PISTELINK_LOCAL_TUNNEL_PORT:-8080}"
ALLOW_SERIAL_ERROR=0
DEBUG=0

usage() {
  cat <<'EOF'
Usage:
  ./start_pistelink_stack.sh [options]
  ./start_pistelink_stack.sh --host thomas@<jetson-ip-or-hostname> [options]

Options:
  --host HOST              Run on a Jetson over SSH, e.g. thomas@192.168.1.50
  --remote-path PATH       Path to this repo on the Jetson (default: /home/thomas/fencing/pistelink)
  --install                Always run deploy/install.sh before starting services
  --no-install             Do not run deploy/install.sh
  --timeout SECONDS        Health-check timeout (default: 120)
  --allow-serial-error     Accept AI-only health if MCU is intentionally disconnected
  --tunnel                 After remote start, hold an SSH tunnel on localhost:8080
  --local-port PORT        Local tunnel port with --tunnel (default: 8080)
  --debug                  Echo shell commands while the starter runs
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing --host value}"
      shift 2
      ;;
    --remote-path)
      REMOTE_PATH="${2:?missing --remote-path value}"
      shift 2
      ;;
    --install)
      RUN_INSTALL=always
      shift
      ;;
    --no-install)
      RUN_INSTALL=never
      shift
      ;;
    --timeout)
      WAIT_TIMEOUT="${2:?missing --timeout value}"
      shift 2
      ;;
    --allow-serial-error)
      ALLOW_SERIAL_ERROR=1
      shift
      ;;
    --tunnel)
      TUNNEL=1
      shift
      ;;
    --local-port)
      LOCAL_TUNNEL_PORT="${2:?missing --local-port value}"
      shift 2
      ;;
    --debug)
      DEBUG=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

START_LOG="${PISTELINK_START_LOG:-/tmp/pistelink-start-$(date +%Y%m%d-%H%M%S).log}"
if [[ "${PISTELINK_NO_TEE:-0}" != "1" ]]; then
  touch "$START_LOG"
  exec > >(tee -a "$START_LOG") 2>&1
  printf '[PISTELINK] start log: %s\n' "$START_LOG"
fi
if [[ "$DEBUG" = "1" ]]; then
  set -x
fi

q() {
  printf '%q' "$1"
}

run_remote() {
  command -v ssh >/dev/null 2>&1 || {
    printf 'ssh is required for --host mode\n' >&2
    exit 1
  }

  local remote_cmd
  remote_cmd="cd $(q "$REMOTE_PATH") && PISTELINK_RUN_INSTALL=$(q "$RUN_INSTALL") PISTELINK_WAIT_TIMEOUT=$(q "$WAIT_TIMEOUT") ./start_pistelink_stack.sh"
  if [[ "$ALLOW_SERIAL_ERROR" = "1" ]]; then
    remote_cmd+=" --allow-serial-error"
  fi
  if [[ "$DEBUG" = "1" ]]; then
    remote_cmd+=" --debug"
  fi

  printf '[PISTELINK] starting Jetson stack on %s\n' "$REMOTE_HOST" >&2
  ssh -tt "$REMOTE_HOST" "$remote_cmd"

  if [[ "$TUNNEL" = "1" ]]; then
    printf '[PISTELINK] stack is up. Holding tunnel: http://127.0.0.1:%s -> Jetson 127.0.0.1:8080\n' "$LOCAL_TUNNEL_PORT" >&2
    printf '[PISTELINK] press Ctrl-C to close the tunnel\n' >&2
    exec ssh -N -L "${LOCAL_TUNNEL_PORT}:127.0.0.1:8080" "$REMOTE_HOST"
  fi
}

if [[ -n "$REMOTE_HOST" ]]; then
  run_remote
  exit 0
fi

sudo_cmd() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

need_host_install() {
  case "$RUN_INSTALL" in
    always)
      return 0
      ;;
    never)
      return 1
      ;;
    auto)
      ;;
    *)
      printf 'Invalid PISTELINK_RUN_INSTALL value: %s\n' "$RUN_INSTALL" >&2
      exit 2
      ;;
  esac

  [[ -r /etc/pistelink/config.toml ]] || return 0
  [[ -r /etc/udev/rules.d/99-pistelink-mcu.rules ]] || return 0
  [[ -r /etc/udev/rules.d/99-pistelink-audio.rules ]] || return 0
  grep -q 'ID_MM_DEVICE_IGNORE' /etc/udev/rules.d/99-pistelink-mcu.rules 2>/dev/null || return 0
  [[ -d /var/lib/pistelink ]] || return 0
  return 1
}

install_or_update_units() {
  chmod +x "${SCRIPT_DIR}/run_pistelink_backend.sh"
  chmod +x "${SCRIPT_DIR}/start_pistelink_stack.sh"
  chmod +x "${AI_BUNDLE_DIR}/run_pistelink_ai_service.sh"

  sudo_cmd cp "${SCRIPT_DIR}/deploy/systemd/pistelink.service" /etc/systemd/system/pistelink.service
  sudo_cmd cp "${AI_DIR}/deploy/systemd/pistelink-ai.service" /etc/systemd/system/pistelink-ai.service
  sudo_cmd systemctl daemon-reload
  sudo_cmd systemctl enable pistelink-ai.service pistelink.service >/dev/null
}

systemd_main_pid() {
  systemctl show -p MainPID --value "$1" 2>/dev/null || true
}

kill_processes_except_service() {
  local pattern="$1"
  local keep_pid="$2"
  local pids=()
  mapfile -t pids < <(pgrep -f "$pattern" 2>/dev/null || true)
  local pid
  for pid in "${pids[@]}"; do
    [[ "$pid" = "$$" || "$pid" = "$keep_pid" || "$pid" = "0" ]] && continue
    printf '[PISTELINK] stopping stale process pid=%s pattern=%s\n' "$pid" "$pattern" >&2
    kill "$pid" 2>/dev/null || true
  done
}

kill_live_stream_except_parent() {
  local keep_parent_pid="$1"
  local pids=()
  mapfile -t pids < <(pgrep -f 'scripts\.live_stream_service' 2>/dev/null || true)
  local pid ppid
  for pid in "${pids[@]}"; do
    [[ "$pid" = "$$" ]] && continue
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    [[ "$ppid" = "$keep_parent_pid" ]] && continue
    printf '[PISTELINK] stopping stale analyzer process pid=%s\n' "$pid" >&2
    kill "$pid" 2>/dev/null || true
  done
}

cleanup_stale_manual_processes() {
  local backend_pid ai_pid
  backend_pid="$(systemd_main_pid pistelink.service)"
  ai_pid="$(systemd_main_pid pistelink-ai.service)"
  kill_processes_except_service 'python .* -m backend\.main|/python .*backend\.main' "$backend_pid"
  kill_processes_except_service 'pistelink_ai_service\.py' "$ai_pid"
  kill_live_stream_except_parent "$ai_pid"
}

wait_for_health() {
  local deadline=$((SECONDS + WAIT_TIMEOUT))
  local body serial ai
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    body="$(curl --noproxy '*' --max-time 2 -fsS http://127.0.0.1:8080/healthz 2>/dev/null || true)"
    if [[ -n "$body" ]]; then
      serial="$("${SCRIPT_DIR}/.venv/bin/python" - "$body" <<'PY'
import json, sys
try:
    print(json.loads(sys.argv[1]).get("serial", ""))
except Exception:
    print("")
PY
)"
      ai="$("${SCRIPT_DIR}/.venv/bin/python" - "$body" <<'PY'
import json, sys
try:
    print(json.loads(sys.argv[1]).get("ai", ""))
except Exception:
    print("")
PY
)"
      if [[ "$ai" = "ok" ]]; then
        if [[ "$serial" = "ok" || "$ALLOW_SERIAL_ERROR" = "1" ]]; then
          printf '%s\n' "$body"
          return 0
        fi
      fi
      printf '[PISTELINK] waiting for health: %s\n' "$body" >&2
    else
      printf '[PISTELINK] waiting for backend HTTP on 127.0.0.1:8080\n' >&2
    fi
    sleep 2
  done
  return 1
}

print_device_summary() {
  printf '[PISTELINK] MCU: ' >&2
  if [[ -e /dev/ttyUSB-mcu ]]; then
    readlink -f /dev/ttyUSB-mcu >&2
  else
    printf 'missing /dev/ttyUSB-mcu\n' >&2
  fi

  printf '[PISTELINK] camera: ' >&2
  local camera=""
  for candidate in /dev/v4l/by-id/*-video-index0 /dev/v4l/by-path/*-video-index0; do
    if [[ -e "$candidate" ]]; then
      camera="$candidate"
      break
    fi
  done
  if [[ -n "$camera" ]]; then
    printf '%s -> %s\n' "$camera" "$(readlink -f "$camera")" >&2
  else
    printf 'missing stable video-index0 path\n' >&2
  fi

  printf '[PISTELINK] audio cards:\n' >&2
  sed -n '1,8p' /proc/asound/cards >&2 2>/dev/null || true
}

main() {
  cd "$SCRIPT_DIR"

  if need_host_install; then
    printf '[PISTELINK] running host setup\n' >&2
    sudo_cmd bash "${SCRIPT_DIR}/deploy/install.sh"
  else
    printf '[PISTELINK] host setup already present\n' >&2
  fi

  install_or_update_units
  cleanup_stale_manual_processes

  printf '[PISTELINK] restarting AI service\n' >&2
  sudo_cmd systemctl restart pistelink-ai.service

  printf '[PISTELINK] restarting backend service\n' >&2
  sudo_cmd systemctl restart pistelink.service

  print_device_summary

  printf '[PISTELINK] waiting for stack health\n' >&2
  if ! wait_for_health; then
    printf '[PISTELINK] stack did not become healthy within %ss\n' "$WAIT_TIMEOUT" >&2
    sudo_cmd systemctl --no-pager --full status pistelink-ai.service pistelink.service >&2 || true
    sudo_cmd journalctl -u pistelink-ai.service -u pistelink.service --no-pager -n 120 >&2 || true
    exit 1
  fi

  printf '[PISTELINK] stack ready: http://127.0.0.1:8080\n' >&2
}

main
