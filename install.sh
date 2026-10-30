#!/bin/sh
# Install or upgrade KenTrackIt on a Raspberry Pi.  From the repo folder:  sudo ./install.sh
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

apt-get install -y python3-serial >/dev/null

id radio-avl >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin radio-avl
usermod -aG dialout radio-avl

mkdir -p /opt/radio-avl /etc/radio-avl /var/lib/radio-avl
rm -rf /opt/radio-avl/radio_avl
cp -r radio_avl /opt/radio-avl/
chown -R radio-avl:radio-avl /var/lib/radio-avl

CFG=/etc/radio-avl/config.json
if [ ! -f "$CFG" ]; then
  cp config.example.json "$CFG"
  # This Pi runs other services: don't start on a port something else already has
  PORT=8090
  while ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; do PORT=$((PORT + 1)); done
  sed -i "s/\"port\": 8090/\"port\": $PORT/" "$CFG"
  # Use the first USB serial adapter, by its stable by-id name
  DEV=$(ls /dev/serial/by-id/* 2>/dev/null | head -1 || true)
  [ -n "$DEV" ] && sed -i "s#/dev/serial/by-id/CHANGE-ME#$DEV#" "$CFG"
  echo "Created $CFG (web port $PORT)"
fi
# The settings page saves changes here, so the service must own it
chown radio-avl:radio-avl /etc/radio-avl "$CFG"
chmod 700 /etc/radio-avl
chmod 600 "$CFG"

cp radio-avl.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable radio-avl >/dev/null
systemctl restart radio-avl
sleep 2
systemctl --no-pager --lines=5 status radio-avl || true

PORT=$(python3 -c "import json;print(json.load(open('$CFG'))['web']['port'])")
echo
echo "Open http://$(hostname -I | awk '{print $1}'):$PORT/settings to finish setting up."
