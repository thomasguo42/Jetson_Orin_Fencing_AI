#!/usr/bin/env bash
# Register the WCH ch341 USB-serial driver with DKMS so it auto-rebuilds on
# kernel upgrades (run as root on the Jetson):
#
#   sudo ./deploy/ch341-dkms/setup-dkms.sh [path/to/ch341ser_linux/driver]
#
# Source resolution: explicit path arg → vendored deploy/CH341SER_LINUX/driver →
# shallow-clone from GitHub. The vendored copy makes this work offline/repeatably.
# Idempotent — re-running re-registers a clean copy. The manual (non-DKMS) build
# is in deploy/README.md.
set -euo pipefail

NAME="ch341"
VERSION="1.8"
REPO="https://github.com/WCHSoftGroup/ch341ser_linux.git"
DEST="/usr/src/${NAME}-${VERSION}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${1:-}"

[ "$(id -u)" -eq 0 ] || { echo "must run as root (sudo)"; exit 1; }

command -v dkms >/dev/null 2>&1 || { echo "==> installing dkms"; apt-get install -y dkms; }

# Obtain the driver source: explicit arg, else vendored copy, else clone.
CLONE_TMP=""
VENDORED="$SCRIPT_DIR/../CH341SER_LINUX/driver"
if [ -z "$SRC_DIR" ]; then
  if [ -f "$VENDORED/Makefile" ]; then
    SRC_DIR="$VENDORED"
    echo "==> using vendored driver source: $SRC_DIR"
  else
    CLONE_TMP="$(mktemp -d)"
    echo "==> cloning $REPO"
    git clone --depth 1 "$REPO" "$CLONE_TMP/ch341ser_linux"
    SRC_DIR="$CLONE_TMP/ch341ser_linux/driver"
  fi
fi
[ -f "$SRC_DIR/Makefile" ] || { echo "no Makefile under '$SRC_DIR' — point me at the WCH driver/ dir"; exit 1; }

# Drop any prior DKMS registration, then lay down a clean source tree + config.
if dkms status -m "$NAME" -v "$VERSION" 2>/dev/null | grep -q .; then
  echo "==> removing existing DKMS registration"
  dkms remove -m "$NAME" -v "$VERSION" --all || true
fi
rm -rf "$DEST"
install -d "$DEST"
cp -r "$SRC_DIR"/. "$DEST"/
cp "$SCRIPT_DIR/dkms.conf" "$DEST/dkms.conf"

echo "==> dkms add / build / install"
dkms add    -m "$NAME" -v "$VERSION"
dkms build  -m "$NAME" -v "$VERSION"
dkms install -m "$NAME" -v "$VERSION"

# Load on boot (same as the manual path).
echo "$NAME" > /etc/modules-load.d/ch341.conf

[ -n "$CLONE_TMP" ] && rm -rf "$CLONE_TMP"

echo "==> done"
dkms status -m "$NAME" -v "$VERSION"
echo "ch341.ko will now rebuild automatically on kernel upgrades."
