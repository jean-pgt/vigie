#!/bin/bash
# Installs Vigie in a Debian 12/13 LXC container (or any Debian machine) with systemd.
# Run as root INSIDE the container, from the repository folder:  bash install/lxc.sh
# Update: git pull, then run the same script again (data, config.yaml and .env are kept).
set -euo pipefail
APP_DIR=/opt/vigie
DATA_DIR=/var/lib/vigie
CONF_DIR=/etc/vigie
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-yaml fping sshpass openssh-client iproute2 curl ca-certificates rsync >/dev/null

id vigie >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin vigie
install -d -o vigie -g vigie -m 700 "$DATA_DIR"
install -d -m 755 "$CONF_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude __pycache__ --exclude manuf "$SOURCE/app/" "$APP_DIR/"
# Vendor database (OUI), optional
curl -sfL -o "$APP_DIR/manuf.tmp" https://www.wireshark.org/download/automated/data/manuf && mv "$APP_DIR/manuf.tmp" "$APP_DIR/manuf" || rm -f "$APP_DIR/manuf.tmp"
python3 -m py_compile "$APP_DIR"/*.py && rm -rf "$APP_DIR/__pycache__"

[ -f "$CONF_DIR/config.yaml" ] || cp "$SOURCE/config.example.yaml" "$CONF_DIR/config.yaml"
if [ ! -f "$CONF_DIR/vigie.env" ]; then cp "$SOURCE/.env.example" "$CONF_DIR/vigie.env"; created=1; fi
chown root:vigie "$CONF_DIR/vigie.env" "$CONF_DIR/config.yaml" && chmod 640 "$CONF_DIR/vigie.env" "$CONF_DIR/config.yaml"

cat > /etc/systemd/system/vigie.service <<UNIT
[Unit]
Description=Vigie - homelab dashboard
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=vigie
Group=vigie
WorkingDirectory=$APP_DIR
EnvironmentFile=$CONF_DIR/vigie.env
Environment=DATA=$DATA_DIR CONFIG=$CONF_DIR/config.yaml PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=on-failure
RestartSec=5
# fping needs raw sockets
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now vigie
systemctl restart vigie
sleep 2; systemctl --no-pager --lines=3 status vigie || true
ip=$(hostname -I | awk '{print $1}')
port=$(sed -n 's/^PORT=//p' "$CONF_DIR/vigie.env"); port=${port:-8080}
echo
echo "Vigie is running: http://$ip:$port"
echo "Settings: $CONF_DIR/config.yaml and $CONF_DIR/vigie.env, then: systemctl restart vigie"
[ "${created:-0}" = 1 ] && echo "Try it with fictional data: add DEMO=1 to $CONF_DIR/vigie.env and restart." || true
