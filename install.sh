#!/bin/bash
# Install bpm2mqtt on Raspberry Pi OS / Debian. Run from the repo directory: sudo ./install.sh
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"

apt-get install -y bluez python3-dbus python3-gi python3-paho-mqtt

install -d /opt/bpm2mqtt
install -m 0755 bpm2mqtt.py /opt/bpm2mqtt/bpm2mqtt.py
install -m 0644 sbm70.py /opt/bpm2mqtt/sbm70.py
install -m 0644 bpm2mqtt.service /etc/systemd/system/bpm2mqtt.service
[ -f /etc/default/bpm2mqtt ] || install -m 0600 bpm2mqtt.env.example /etc/default/bpm2mqtt

# Optional: lower the LE scan duty during discovery (units of 0.625 ms): 15 ms every 60 ms = 25 %.
# Matters on boards where Wi-Fi and Bluetooth share one chip (Raspberry Pi 3/4/Zero W).
if [ "${LOW_SCAN_DUTY:-1}" = 1 ]; then
  sed -i 's/^#\?ScanIntervalDiscovery=.*/ScanIntervalDiscovery=96/; s/^#\?ScanWindowDiscovery=.*/ScanWindowDiscovery=24/' /etc/bluetooth/main.conf
fi

rfkill unblock bluetooth 2>/dev/null || true
systemctl enable --now bluetooth
systemctl restart bluetooth
systemctl daemon-reload
systemctl enable bpm2mqtt

echo
echo "Installed. Next:"
echo "  1. pair the monitor once (README, 'Pairing')"
echo "  2. set SBM70_MAC and MQTT_* in /etc/default/bpm2mqtt"
echo "  3. sudo systemctl start bpm2mqtt && journalctl -u bpm2mqtt -f"
