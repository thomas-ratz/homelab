#!/usr/bin/env bash
set -euo pipefail

# One-time setup for the Fujifilm → Immich auto-import pipeline.
# Run with: sudo bash photos/setup.sh

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root. Use: sudo bash $0"
    exit 1
fi

HOMELAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_USER="${SUDO_USER:-thopi}"

echo "=== Installing gphoto2 ==="
apt-get update -qq
apt-get install -y gphoto2
echo "gphoto2 version: $(gphoto2 --version | head -1)"

echo ""
echo "=== Installing Node.js (for Immich CLI) ==="
if ! command -v node &>/dev/null; then
    if ! command -v fnm &>/dev/null && ! command -v nvm &>/dev/null; then
        echo "Installing Node.js via NodeSource..."
        curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
        apt-get install -y nodejs
    fi
fi
echo "node version: $(node --version 2>/dev/null || echo 'not found')"
echo "npm version: $(npm --version 2>/dev/null || echo 'not found')"

echo ""
echo "=== Installing Immich CLI ==="
if command -v npm &>/dev/null; then
    npm i -g @immich/cli 2>&1 || echo "WARN: Immich CLI install failed. Install manually: npm i -g @immich/cli"
fi

echo ""
echo "=== Creating udev rule ==="
cat > /etc/udev/rules.d/99-fuji-import.rules << 'UDEV'
# Auto-trigger photo import when a Fujifilm camera is connected via USB
# Fujifilm USB vendor ID: 04cb
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="04cb", TAG+="systemd", ENV{SYSTEMD_WANTS}="photo-import.service"
UDEV
udevadm control --reload-rules
echo "udev rule installed at /etc/udev/rules.d/99-fuji-import.rules"

echo ""
echo "=== Creating systemd service ==="
cat > /etc/systemd/system/photo-import.service << EOF
[Unit]
Description=Import photos from Fujifilm camera via USB
After=network.target

[Service]
Type=oneshot
User=${SCRIPT_USER}
WorkingDirectory=${HOMELAB_DIR}
ExecStartPre=/bin/sleep 3
ExecStart=${HOMELAB_DIR}/photos/import-photos.sh
Environment=HOME=/home/${SCRIPT_USER}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "systemd service installed at /etc/systemd/system/photo-import.service"

echo ""
echo "=== Setting up Python venv for web app ==="
VENV_DIR="${HOMELAB_DIR}/photos/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "${SCRIPT_USER}" python3 -m venv "$VENV_DIR"
fi
sudo -u "${SCRIPT_USER}" "$VENV_DIR/bin/pip" install -q flask
echo "Flask installed in $VENV_DIR"

echo ""
echo "=== Creating web app systemd service ==="
cat > /etc/systemd/system/photo-import-web.service << EOF
[Unit]
Description=Photo Import Control Panel
After=network.target

[Service]
Type=simple
User=${SCRIPT_USER}
WorkingDirectory=${HOMELAB_DIR}/photos
ExecStart=${VENV_DIR}/bin/python3 ${HOMELAB_DIR}/photos/app.py
Environment=HOME=/home/${SCRIPT_USER}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable photo-import-web.service
systemctl restart photo-import-web.service
echo "Web app service installed and started"

echo ""
echo "=== Creating import directory ==="
mkdir -p "${HOMELAB_DIR}/photos/import"
chown -R "${SCRIPT_USER}:${SCRIPT_USER}" "${HOMELAB_DIR}/photos"

echo ""
echo "==========================================="
echo "  Setup complete!"
echo "==========================================="
echo ""
echo "Next steps:"
echo "  1. Generate an Immich API key:"
echo "     Open photos.thopi.ts -> User Settings -> API Keys -> New API Key"
echo ""
echo "  2. Add the key to your .env file:"
echo "     echo 'IMMICH_API_KEY=your-key-here' >> ${HOMELAB_DIR}/.env"
echo ""
echo "  3. Web UI available at: http://import.thopi.ts"
echo ""
echo "  4. Or just plug the camera in -- the udev rule will auto-trigger!"
echo ""
