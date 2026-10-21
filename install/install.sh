#!/usr/bin/env bash
# Install TUA on Raspberry Pi OS or another Debian-family system.
#   sudo ./install/install.sh
#   ./install/install.sh --user
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="system"
if [[ "${1:-}" == "--user" ]]; then
  MODE="user"
fi

if [[ "$MODE" == "system" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run sudo ./install/install.sh, or ./install/install.sh --user to install into your home directory." >&2
    exit 1
  fi
  PREFIX="${RUTKEEPER_PREFIX:-/opt/rutkeeper}"
  DATA="${RUTKEEPER_DATA_DIR:-/var/lib/rutkeeper}"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3 python3-venv ca-certificates
  fi
else
  PREFIX="${RUTKEEPER_PREFIX:-$HOME/.local/rutkeeper}"
  DATA="${RUTKEEPER_DATA_DIR:-$HOME/.local/share/rutkeeper}"
fi

mkdir -p "$PREFIX" "$DATA/backups"
if [[ -d /boot/firmware ]]; then
  FILE_ROOT="/boot/firmware"
else
  FILE_ROOT="$DATA"
fi
mkdir -p "$FILE_ROOT/Firmware" "$FILE_ROOT/Config 1" "$FILE_ROOT/Config 2" "$FILE_ROOT/api"
chmod 0775 "$FILE_ROOT/Firmware" "$FILE_ROOT/Config 1" "$FILE_ROOT/Config 2" "$FILE_ROOT/api" || true
if [[ "$MODE" == "system" ]] && command -v systemctl >/dev/null 2>&1; then
  systemctl stop rutkeeper.service 2>/dev/null || true
fi
rm -rf "$PREFIX/rutkeeper"
cp -a "$ROOT/rutkeeper" "$PREFIX/rutkeeper"
cp "$ROOT/requirements.txt" "$PREFIX/requirements.txt"

if [[ ! -x "$PREFIX/.venv/bin/python" ]]; then
  python3 -m venv "$PREFIX/.venv"
fi
"$PREFIX/.venv/bin/pip" install --upgrade pip
"$PREFIX/.venv/bin/pip" install -r "$PREFIX/requirements.txt"

if [[ "$MODE" == "system" ]]; then
  if ! id rutkeeper >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin rutkeeper
  fi
  chown -R rutkeeper:rutkeeper "$PREFIX" "$DATA"
  cat > /etc/systemd/system/rutkeeper.service <<EOF
[Unit]
Description=TUA Teltonika firmware and config keeper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rutkeeper
Group=rutkeeper
WorkingDirectory=$PREFIX
Environment=RUTKEEPER_DATA=$DATA
Environment=RUTKEEPER_FILES=$FILE_ROOT
Environment=RUTKEEPER_HOST=0.0.0.0
Environment=RUTKEEPER_PORT=8741
Environment=RUTKEEPER_TLS=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PREFIX/.venv/bin/python -m rutkeeper
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now rutkeeper.service
  PI_IP="$(hostname -I | awk '{print $1}')"
  echo
  echo "TUA is running at https://${PI_IP}:8741"
  echo "Accept the self-signed certificate once. Workbench and live label scan use that same address."
  echo "Data directory: $DATA"
else
  echo
  echo "Installed into $PREFIX"
  echo "Start it with:"
  echo "  RUTKEEPER_DATA=$DATA RUTKEEPER_HOST=0.0.0.0 RUTKEEPER_PORT=8741 RUTKEEPER_TLS=1 $PREFIX/.venv/bin/python -m rutkeeper"
fi

echo "Keep this host off the public internet. It stores router admin passwords."
