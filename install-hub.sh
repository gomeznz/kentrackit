#!/bin/sh
# Install or upgrade the KenTrackIt hosted service (many companies on one server).
# On an Ubuntu/Debian server or VM, from the repo folder:  sudo ./install-hub.sh
# Not for the site Pis: they use install.sh, and send their radio data here as gateways.
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

apt-get install -y python3 python3-numpy python3-httpx python3-h2 python3-cryptography iproute2 sqlite3 >/dev/null

id kentrackit-hub >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kentrackit-hub
mkdir -p /opt/kentrackit-hub /var/lib/kentrackit-hub
rm -rf /opt/kentrackit-hub/radio_avl
cp -r radio_avl /opt/kentrackit-hub/
chown -R kentrackit-hub:kentrackit-hub /var/lib/kentrackit-hub
chmod 700 /var/lib/kentrackit-hub

UNIT=/etc/systemd/system/kentrackit-hub.service
if [ -f "$UNIT" ]; then
  PORT=$(sed -n 's/.*--port \([0-9]*\).*/\1/p' "$UNIT")       # keep the port it already has
else
  PORT=8100
  while ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; do PORT=$((PORT + 1)); done
fi
sed "s/--port 8100/--port $PORT/" kentrackit-hub.service > "$UNIT"
systemctl daemon-reload
systemctl enable kentrackit-hub >/dev/null
systemctl restart kentrackit-hub
sleep 2
systemctl --no-pager --lines=5 status kentrackit-hub || true

echo
echo "The hosted service is on port $PORT."
echo "Open http://$(hostname -I | awk '{print $1}'):$PORT/hub to create the first ASI administrator,"
echo "then point a Cloudflare tunnel hostname at http://localhost:$PORT (see docs/hosted.md)."
