#!/usr/bin/env bash
# One-time host setup for PisteLink (run as root on the Jetson):
#   sudo ./deploy/install.sh
#
# Idempotent. Creates the runtime directories; removes brltty and installs mpg123
# (best-effort, warns offline); installs the udev rules, the tmpfiles.d entry and
# the config template; generates the SFTP upload key. With PISTELINK_KIOSK=1 it
# also installs and enables the kiosk USER unit. It does NOT build/run the Docker
# container or enable the backend system service — see deploy/README.md for those
# (kept explicit so you choose Docker vs bare systemd).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "must run as root (sudo)"; exit 1; }

detect_owner() {
  if [ -n "${PISTELINK_OWNER:-}" ]; then
    printf '%s\n' "$PISTELINK_OWNER"
    return
  fi
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" >/dev/null 2>&1; then
    printf '%s\n' "$SUDO_USER"
    return
  fi
  if id nvidia >/dev/null 2>&1; then
    printf '%s\n' "nvidia"
    return
  fi
  if [ -n "${LOGNAME:-}" ] && [ "$LOGNAME" != "root" ] && id "$LOGNAME" >/dev/null 2>&1; then
    printf '%s\n' "$LOGNAME"
    return
  fi
  printf '%s\n' "root"
}

OWNER="$(detect_owner)"
id "$OWNER" >/dev/null 2>&1 || { echo "owner user '$OWNER' not found (set PISTELINK_OWNER)"; exit 1; }
OWNER_GROUP="$(id -gn "$OWNER")"

echo "==> service user/group"
echo "    owner: $OWNER:$OWNER_GROUP"
for group in dialout audio video render; do
  if getent group "$group" >/dev/null 2>&1; then
    usermod -aG "$group" "$OWNER" || true
  fi
done

echo "==> runtime directories (owner $OWNER)"
install -d -m 0750 -o "$OWNER" -g "$OWNER_GROUP" /etc/pistelink
install -d -m 0750 -o "$OWNER" -g "$OWNER_GROUP" /var/lib/pistelink
install -d -m 0770 -o "$OWNER" -g "$OWNER_GROUP" /run/pistelink

echo "==> brltty (braille daemon hijacks the CH340 1a86:7523 — remove it)"
# brltty ships a udev rule that claims 1a86:7523, so the CH340 never gets a tty
# (dmesg: "interface 0 claimed by usb_ch341 while 'brltty' sets config #1").
# On this appliance brltty is useless, so purge it outright. Idempotent; a missing
# package or no-network apt is a warning, not a hard failure.
if dpkg-query -W -f='${Status}' brltty 2>/dev/null | grep -q "install ok installed"; then
  if apt-get remove -y --purge brltty; then
    echo "    brltty removed"
  else
    echo "    WARNING: failed to remove brltty (no network?) — run 'apt-get remove -y --purge brltty' manually" >&2
  fi
else
  echo "    brltty not installed — nothing to do"
fi

echo "==> mpg123 (audio player for match sounds)"
# The backend plays match sounds via mpg123 (audio.py). Without it the backend
# runs but silently skips playback. Idempotent; no-network apt warns instead of
# aborting (match flow still works, just no sounds until installed).
if command -v mpg123 >/dev/null 2>&1; then
  echo "    mpg123 already installed"
elif apt-get install -y mpg123; then
  echo "    mpg123 installed"
else
  echo "    WARNING: failed to install mpg123 (no network?) — run 'apt-get install -y mpg123' manually; no match sounds until then" >&2
fi

echo "==> udev rules (CH340 -> /dev/ttyUSB-mcu; USB sound card -> ALSA name 'pistelink')"
install -m 0644 "$SCRIPT_DIR/udev/99-pistelink-mcu.rules" /etc/udev/rules.d/99-pistelink-mcu.rules
install -m 0644 "$SCRIPT_DIR/udev/99-pistelink-audio.rules" /etc/udev/rules.d/99-pistelink-audio.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true
udevadm trigger --subsystem-match=sound || true
CH340_COUNT="$(find /sys/bus/usb/devices -maxdepth 2 -name idVendor -exec sh -c '
  count=0
  for vendor_path do
    dir=$(dirname "$vendor_path")
    if [ "$(cat "$dir/idVendor" 2>/dev/null)" = "1a86" ] && [ "$(cat "$dir/idProduct" 2>/dev/null)" = "7523" ]; then
      count=$((count + 1))
    fi
  done
  printf "%s" "$count"
' sh {} + 2>/dev/null || printf '0')"
if [ "${CH340_COUNT:-0}" -gt 1 ]; then
  echo "    WARNING: more than one CH340 1a86:7523 device is attached; add a serial/path match to 99-pistelink-mcu.rules" >&2
fi

echo "==> tmpfiles.d (recreate /run/pistelink on every boot)"
sed "s/nvidia nvidia/$OWNER $OWNER_GROUP/g" "$SCRIPT_DIR/tmpfiles/pistelink.conf" >/etc/tmpfiles.d/pistelink.conf
systemd-tmpfiles --create /etc/tmpfiles.d/pistelink.conf

echo "==> config template"
if [ -f /etc/pistelink/config.toml ]; then
  echo "    /etc/pistelink/config.toml exists — left untouched"
else
  install -m 0600 -o "$OWNER" -g "$OWNER_GROUP" "$SCRIPT_DIR/config.example.toml" /etc/pistelink/config.toml
  sed -i "s#/home/nvidia/.ssh/id_ed25519#/home/$OWNER/.ssh/id_ed25519#g" /etc/pistelink/config.toml
  echo "    installed /etc/pistelink/config.toml (SFTP server pre-filled; upload key"
  echo "    expected at /home/$OWNER/.ssh/id_ed25519 — see step below)"
fi

if [ "${PISTELINK_KIOSK:-0}" = "1" ]; then
  echo "==> kiosk (full-screen Chromium on boot, opt-in via PISTELINK_KIOSK=1)"
  UID_OWNER="$(id -u "$OWNER")"
  USER_UNIT_DIR="/home/$OWNER/.config/systemd/user"
  install -d -m 0755 -o "$OWNER" -g "$OWNER_GROUP" "$USER_UNIT_DIR"
  install -m 0644 -o "$OWNER" -g "$OWNER_GROUP" \
    "$SCRIPT_DIR/systemd/pistelink-kiosk.service" "$USER_UNIT_DIR/pistelink-kiosk.service"
  # Linger lets the user's systemd run (and the kiosk start) without an interactive login.
  loginctl enable-linger "$OWNER" || true
  if sudo -u "$OWNER" XDG_RUNTIME_DIR="/run/user/$UID_OWNER" \
       systemctl --user enable pistelink-kiosk.service 2>/dev/null; then
    echo "    kiosk unit enabled (starts with the graphical session)"
  else
    echo "    kiosk unit installed but not enabled now (no user session yet) — enable it"
    echo "    from a desktop session of '$OWNER': systemctl --user enable --now pistelink-kiosk.service" >&2
  fi
  # snap installs to /snap/bin, which is not on root's secure_path — check it too.
  command -v chromium-browser >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 \
    || [ -x /snap/bin/chromium ] \
    || echo "    WARNING: no chromium/chromium-browser found — install one (e.g. snap install chromium)" >&2
  echo "    NOTE: the kiosk only appears if the desktop AUTO-LOGS IN '$OWNER' at boot;"
  echo "          enable display-manager autologin separately if it is not already on."
else
  echo "==> kiosk: skipped (set PISTELINK_KIOSK=1 to install the full-screen UI)"
fi

echo "==> SFTP upload key"
KEY="/home/$OWNER/.ssh/id_ed25519"
if [ -f "$KEY" ]; then
  echo "    $KEY exists — left untouched"
else
  install -d -m 0700 -o "$OWNER" -g "$OWNER_GROUP" "/home/$OWNER/.ssh"
  sudo -u "$OWNER" ssh-keygen -t ed25519 -N "" -C "pistelink@$(hostname)" -f "$KEY"
  echo "    generated $KEY"
fi
echo "    register this public key on the upload server's authorized_keys:"
echo "      $(cat "$KEY.pub")"

cat <<EOF

Host setup done. Next — bare systemd + conda (see deploy/README.md):
  1. Build frontend on a networked machine; copy backend/ sound/ frontend/dist/
     requirements.txt deploy/ to /opt/pistelink/.
  2. Miniforge (aarch64), then create the env with an ABSOLUTE path (-p, not -n:
     on a root-installed /opt/miniforge3 a non-root user can't write envs/ and
     conda silently builds it under ~/.conda/envs, mismatching the service
     ExecStart and failing with status=203/EXEC):
       sudo /opt/miniforge3/bin/conda create -y -p /opt/miniforge3/envs/pistelink python=3.11
       sudo /opt/miniforge3/envs/pistelink/bin/pip install -r /opt/pistelink/requirements.txt \\
         -i https://pypi.tuna.tsinghua.edu.cn/simple
     (mpg123 was installed by this script above.)
  3. SFTP uses public-key auth. The key was generated above (if missing) —
     register the printed public key on the upload server's authorized_keys
     for the SFTP user before the first upload will succeed.
  4. sudo cp $SCRIPT_DIR/systemd/pistelink.service /etc/systemd/system/
     sudo systemctl enable --now pistelink.service
EOF
