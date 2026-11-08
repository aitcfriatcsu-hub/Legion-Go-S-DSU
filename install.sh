#!/usr/bin/env bash
# legion-dsu installer for the Legion Go S on SteamOS.
#
# Usage:
#   bash install.sh              install (uses ./legion-dsu.py if present, else downloads it)
#   bash install.sh --uninstall  remove everything this installer set up
#
# One-liner (after uploading to GitHub, edit REPO_RAW below first):
#   bash <(curl -fsSL https://raw.githubusercontent.com/aitcfriatcsu-hub/Legion-Go-S-DSU/main/install.sh)

set -euo pipefail

# >>> EDIT THIS after you upload the repo <<<
REPO_RAW="https://raw.githubusercontent.com/aitcfriatcsu-hub/Legion-Go-S-DSU/main"

BIN_DIR="$HOME/.local/bin"
BIN_PATH="$BIN_DIR/legion-dsu.py"
SERVICE_NAME="legion-dsu"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
UDEV_FILE="/etc/udev/rules.d/99-legion-dsu.rules"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  die "run this as your normal user (not root / not with sudo); it asks for sudo when needed"
fi

if [ "${1:-}" = "--uninstall" ]; then
  say "Stopping and removing the service..."
  systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$BIN_PATH"
  systemctl --user daemon-reload 2>/dev/null || true
  say "Removing the udev rule (needs sudo)..."
  sudo rm -f "$UDEV_FILE"
  sudo udevadm control --reload-rules || true
  say "Uninstalled."
  exit 0
fi

echo "============================================"
echo " legion-dsu installer"
echo "============================================"

command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found"

# Is the virtual Deck-style controller (28DE:12FF) there?
found=0
for f in /sys/class/hidraw/hidraw*/device/uevent; do
  [ -e "$f" ] || continue
  if grep -qiE '^HID_ID=.*:0*28DE:0*12FF$' "$f" 2>/dev/null; then found=1; break; fi
done
if [ "$found" -eq 0 ]; then
  echo "warning: no controller with USB ID 28DE:12FF found."
  echo "         InputPlumber may not be running, or this device presents a different ID."
  if [ "${FORCE:-0}" != "1" ]; then
    die "aborting. Re-run with FORCE=1 to install anyway."
  fi
fi

# The old IIO bridge uses the same port (26760)
if systemctl --user list-unit-files iio-dsu-bridge.service >/dev/null 2>&1 \
   && systemctl --user is-enabled iio-dsu-bridge >/dev/null 2>&1; then
  say "Disabling iio-dsu-bridge (same port)..."
  systemctl --user disable --now iio-dsu-bridge || true
fi

say "Installing the script to $BIN_PATH..."
mkdir -p "$BIN_DIR"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
if [ -f "$SELF_DIR/legion-dsu.py" ]; then
  cp "$SELF_DIR/legion-dsu.py" "$BIN_PATH"
else
  curl -fsSL "$REPO_RAW/legion-dsu.py" -o "$BIN_PATH" \
    || die "could not download legion-dsu.py from $REPO_RAW (did you edit REPO_RAW?)"
fi
# strip Windows line endings if the file picked any up
sed -i 's/\r$//' "$BIN_PATH"
chmod +x "$BIN_PATH"
python3 -m py_compile "$BIN_PATH" || die "the downloaded script is not valid Python"

say "Giving user '$USER' access to the controller (needs sudo)..."
echo "SUBSYSTEM==\"hidraw\", KERNELS==\"0003:28DE:12FF.*\", OWNER=\"$USER\", MODE=\"0660\"" | sudo tee "$UDEV_FILE" >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --action=add

say "Creating the user service..."
mkdir -p "$(dirname "$SERVICE_FILE")"
cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=Legion Go S DSU motion server

[Service]
ExecStart=/usr/bin/python3 %h/.local/bin/legion-dsu.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

if command -v loginctl >/dev/null 2>&1; then
  say "Enabling auto-start without login (needs sudo)..."
  sudo loginctl enable-linger "$USER" 2>/dev/null || true
fi

sleep 2
echo ""
echo "============================================"
echo "  Done."
echo "============================================"
systemctl --user --no-pager status "$SERVICE_NAME" 2>&1 | head -8 || true
echo ""
echo "In your emulator, set the motion/DSU server to 127.0.0.1 port 26760."
echo "Logs:      journalctl --user -u $SERVICE_NAME -f"
echo "Uninstall: bash install.sh --uninstall"
