#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

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

choose_storage_root() {
  if [[ -n "${PISTELINK_STORAGE_ROOT:-}" ]]; then
    mkdir -p "${PISTELINK_STORAGE_ROOT}/matches"
    printf '%s\n' "$PISTELINK_STORAGE_ROOT"
    return
  fi

  if mkdir -p /var/lib/pistelink/matches 2>/dev/null && [[ -w /var/lib/pistelink ]]; then
    printf '%s\n' "/var/lib/pistelink"
    return
  fi

  if [[ -n "${HOME:-}" ]]; then
    local home_root="${HOME}/.local/share/pistelink"
    mkdir -p "${home_root}/matches"
    printf '%s\n' "$home_root"
    return
  fi

  local fallback_root="${PISTELINK_RUNTIME_DIR}/storage"
  mkdir -p "${fallback_root}/matches"
  printf '%s\n' "$fallback_root"
}

toml_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

read_http_bind() {
  "$PYTHON_BIN" - "$PISTELINK_CONFIG" "${PISTELINK_HTTP_HOST:-127.0.0.1}" "${PISTELINK_HTTP_PORT:-8080}" <<'PY'
import sys

path, default_host, default_port = sys.argv[1:4]
host = default_host or "127.0.0.1"
port = default_port or "8080"

try:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    with open(path, "rb") as f:
        config = tomllib.load(f)
    http = config.get("http", {})
    host = str(http.get("host", host) or host)
    port = str(int(http.get("port", port)))
except Exception:
    pass

print(host, port)
PY
}

listener_pids_for_port() {
  "$PYTHON_BIN" - "${1:?port}" "$$" <<'PY'
import os
import sys

port = int(sys.argv[1])
self_pid = int(sys.argv[2])
listen_state = "0A"
socket_inodes = set()

for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(table, "r", encoding="utf-8").read().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        fields = line.split()
        if len(fields) < 10:
            continue
        local_addr = fields[1]
        state = fields[3]
        inode = fields[9]
        try:
            local_port = int(local_addr.rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if local_port == port and state == listen_state:
            socket_inodes.add(inode)

if not socket_inodes:
    sys.exit(0)

pids = set()
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    pid = int(name)
    if pid == self_pid:
        continue
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        continue
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in socket_inodes:
            pids.add(pid)
            break

for pid in sorted(pids):
    print(pid)
PY
}

kill_conflicting_http_listeners() {
  if [[ "${PISTELINK_KILL_CONFLICTS:-1}" != "1" ]]; then
    return
  fi

  local host="$1"
  local port="$2"
  local pids=()
  mapfile -t pids < <(listener_pids_for_port "$port")
  if [[ "${#pids[@]}" -eq 0 ]]; then
    return
  fi

  local pid cmd
  for pid in "${pids[@]}"; do
    if [[ -r "/proc/$pid/cmdline" ]]; then
      cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
    else
      cmd="(unknown command)"
    fi
    printf '[PISTELINK] killing process already listening on %s:%s: pid=%s %s\n' "$host" "$port" "$pid" "$cmd" >&2
    kill "$pid" 2>/dev/null || true
  done

  local deadline=$((SECONDS + 5))
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    mapfile -t pids < <(listener_pids_for_port "$port")
    [[ "${#pids[@]}" -eq 0 ]] && return
    sleep 0.2
  done

  mapfile -t pids < <(listener_pids_for_port "$port")
  for pid in "${pids[@]}"; do
    printf '[PISTELINK] force killing process still listening on %s:%s: pid=%s\n' "$host" "$port" "$pid" >&2
    kill -9 "$pid" 2>/dev/null || true
  done
}

running_under_systemd_service() {
  [[ -n "${INVOCATION_ID:-}" || "${PPID:-}" = "1" ]]
}

stop_systemd_backend_for_manual_takeover() {
  if running_under_systemd_service; then
    return
  fi
  if [[ "${PISTELINK_STOP_SYSTEMD_ON_MANUAL_START:-1}" != "1" ]]; then
    return
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  if ! systemctl --quiet is-active pistelink.service 2>/dev/null; then
    return
  fi

  printf '[PISTELINK] pistelink.service is active; stopping it before manual takeover\n' >&2
  if [[ -t 0 ]]; then
    sudo systemctl stop pistelink.service
  else
    sudo -n systemctl stop pistelink.service
  fi

  local deadline=$((SECONDS + 10))
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    if ! systemctl --quiet is-active pistelink.service 2>/dev/null; then
      return
    fi
    sleep 0.2
  done

  printf '[PISTELINK] failed to stop pistelink.service; run: sudo systemctl stop pistelink.service\n' >&2
  exit 1
}

export PISTELINK_RUNTIME_DIR="$(choose_runtime_dir)"
export PISTELINK_FRONTEND_DIR="${PISTELINK_FRONTEND_DIR:-${SCRIPT_DIR}/frontend/dist}"
export PISTELINK_SOUND_DIR="${PISTELINK_SOUND_DIR:-${SCRIPT_DIR}/sound}"
AI_SOCKET="${PISTELINK_AI_SOCKET:-${PISTELINK_RUNTIME_DIR}/ai.sock}"
STORAGE_ROOT="$(choose_storage_root)"
GENERATE_CONFIG=0

if [[ -n "${PISTELINK_CONFIG:-}" ]]; then
  if [[ -r "$PISTELINK_CONFIG" ]]; then
    :
  else
    requested_config="$PISTELINK_CONFIG"
    requested_dir="$(dirname "$requested_config")"
    if { [[ ! -e "$requested_config" ]] && mkdir -p "$requested_dir" 2>/dev/null && [[ -w "$requested_dir" ]]; } || [[ -w "$requested_config" ]]; then
      GENERATE_CONFIG=1
    else
      printf '[PISTELINK] backend config %s is not writable/readable; using runtime config\n' "$requested_config" >&2
      export PISTELINK_CONFIG="${PISTELINK_RUNTIME_DIR}/config.toml"
      GENERATE_CONFIG=1
    fi
  fi
elif [[ -r /etc/pistelink/config.toml ]]; then
  export PISTELINK_CONFIG="/etc/pistelink/config.toml"
else
  export PISTELINK_CONFIG="${PISTELINK_RUNTIME_DIR}/config.toml"
  GENERATE_CONFIG=1
fi

if [[ "$GENERATE_CONFIG" = "1" ]]; then
  cat >"$PISTELINK_CONFIG" <<EOF
[serial]
device = "$(toml_escape "${PISTELINK_SERIAL_DEVICE:-/dev/ttyUSB-mcu}")"
baud = ${PISTELINK_SERIAL_BAUD:-115200}

[signal]
video_sync_offset_ms = 0

[ai]
enabled = true
socket = "$(toml_escape "$AI_SOCKET")"
reconnect_min_s = 1
reconnect_max_s = 30
heartbeat_s = 2
heartbeat_timeout_s = 6
result_timeout_s = ${PISTELINK_AI_RESULT_TIMEOUT_S:-30}

[storage]
root = "$(toml_escape "$STORAGE_ROOT")"

[audio]
device = "$(toml_escape "${PISTELINK_AUDIO_DEVICE:-auto}")"
playback_timeout_s = ${PISTELINK_AUDIO_TIMEOUT_S:-10}

[upload]
host = ""
port = 22
username = ""
password = ""
private_key = ""
key_passphrase = ""
base_path = "/"
timeout_s = 60
post_upload_action = "delete_video_only"

[http]
host = "${PISTELINK_HTTP_HOST:-127.0.0.1}"
port = ${PISTELINK_HTTP_PORT:-8080}

[ui]
locale = "zh-CN"

[kiosk]
enabled = false
EOF
fi

read -r HTTP_HOST HTTP_PORT < <(read_http_bind)
stop_systemd_backend_for_manual_takeover
kill_conflicting_http_listeners "$HTTP_HOST" "$HTTP_PORT"

printf '[PISTELINK] backend config: %s\n' "$PISTELINK_CONFIG" >&2
printf '[PISTELINK] backend runtime: %s\n' "$PISTELINK_RUNTIME_DIR" >&2
printf '[PISTELINK] backend storage: %s\n' "$STORAGE_ROOT" >&2
printf '[PISTELINK] backend AI socket: %s\n' "$AI_SOCKET" >&2
printf '[PISTELINK] backend HTTP bind: %s:%s\n' "$HTTP_HOST" "$HTTP_PORT" >&2

cd "$SCRIPT_DIR"
exec "$PYTHON_BIN" -m backend.main "$@"
