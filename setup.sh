#!/usr/bin/env bash
# Jellyfin Media Auto-Downloader — one-command installer
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="jellyfin-downloader"
PYTHON="${PYTHON:-python3}"

echo "=== Jellyfin Media Auto-Downloader Setup ==="
echo ""

# 1. Check Python version
PYTHON_VER=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "Python: $PYTHON_VER"

# 2. Create virtual environment
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
  echo "Creating virtual environment…"
  $PYTHON -m venv "$SCRIPT_DIR/.venv"
fi

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
VENV_PIP="$SCRIPT_DIR/.venv/bin/pip"

# 3. Install dependencies
echo "Installing Python dependencies…"
$VENV_PIP install --upgrade pip -q
$VENV_PIP install -r "$SCRIPT_DIR/requirements.txt" -q
echo "Dependencies installed."

# 4. Check config
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
  echo ""
  echo "ERROR: config.json not found."
  echo "  Edit config.json with your credentials and library paths before continuing."
  exit 1
fi

echo ""
echo "Config file: OK"

# 5. Install as systemd service (Linux only)
if command -v systemctl &>/dev/null; then
  echo ""
  echo "Installing systemd service: $SERVICE_NAME"
  SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Jellyfin Media Auto-Downloader
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_PYTHON $SCRIPT_DIR/downloader.py
Restart=always
RestartSec=60
StandardOutput=append:$SCRIPT_DIR/downloader.log
StandardError=append:$SCRIPT_DIR/downloader.log

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME"
  sudo systemctl start "$SERVICE_NAME"
  echo ""
  echo "Service installed and started."
  echo "  Status:  systemctl status $SERVICE_NAME"
  echo "  Logs:    journalctl -u $SERVICE_NAME -f"
  echo "  Logfile: $SCRIPT_DIR/downloader.log"
else
  echo ""
  echo "systemd not found — run manually with:"
  echo "  $VENV_PYTHON $SCRIPT_DIR/downloader.py"
  echo ""
  echo "On Windows, use Task Scheduler or run in a terminal:"
  echo "  .venv\\Scripts\\python.exe downloader.py"
fi

echo ""
echo "=== Setup complete ==="
