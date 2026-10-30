#!/bin/sh
# Install or upgrade Radio AVL on a Raspberry Pi.  From the unpacked folder:  sudo ./install.sh
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

apt-get install -y python3-serial >/dev/null

id radio-avl >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin radio-avl
usermod -aG dialout radio-avl

mkdir -p /opt/radio-avl /etc/radio-avl /var/lib/radio-avl
rm -rf /opt/radio-avl/radio_avl
cp -r radio_avl /opt/radio-avl/
chown -R radio-avl:radio-avl /var/lib/radio-avl

if [ ! -f /etc/radio-avl/config.json ]; then
  cp config.example.json /etc/radio-avl/config.json
  echo "Created /etc/radio-avl/config.json"
fi
chmod 640 /etc/radio-avl/config.json
chgrp radio-avl /etc/radio-avl/config.json

cp radio-avl.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable radio-avl >/dev/null
systemctl restart radio-avl
sleep 2
systemctl --no-pager --lines=5 status radio-avl || true

echo
echo "Serial adapters on this Pi (put the radio's one in /etc/radio-avl/config.json):"
ls -l /dev/serial/by-id/ 2>/dev/null || echo "  none found"
echo
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8090"
