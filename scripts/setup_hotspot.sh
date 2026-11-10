#!/usr/bin/env bash
# ==============================================================================
# DVM Nexus - NetworkManager Failover Hotspot & Watchdog Setup Script
# Target: Raspberry Pi 5 / Debian / Raspbian Bookworm
# ==============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "[ERROR] This setup script must be run as root: sudo bash setup_hotspot.sh"
   exit 1
fi

echo "=========================================================="
echo "  DVM Nexus - Field Hotspot & Failover AP Setup"
echo "=========================================================="

# 1. Setup Dedicated Failover Hotspot Connection
HOTSPOT_CON="Field-Hotspot"
HOTSPOT_SSID="MMDVM-Field-AP"
HOTSPOT_PASS="FieldPassword123"

echo "[1/4] Configuring NetworkManager Hotspot connection '${HOTSPOT_CON}'..."

# Check if already exists, remove or create fresh
if nmcli connection show "${HOTSPOT_CON}" >/dev/null 2>&1; then
    echo "  -> Updating existing connection '${HOTSPOT_CON}'..."
    nmcli connection modify "${HOTSPOT_CON}" \
        802-11-wireless.ssid "${HOTSPOT_SSID}" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        802-11-wireless.channel 6 \
        802-11-wireless-security.key-mgmt wpa-psk \
        802-11-wireless-security.psk "${HOTSPOT_PASS}" \
        ipv4.method shared \
        ipv4.addresses 10.42.0.1/24 \
        ipv6.method ignore \
        connection.autoconnect no \
        connection.autoconnect-priority 1
else
    echo "  -> Creating new connection '${HOTSPOT_CON}'..."
    nmcli connection add \
        type wifi \
        ifname wlan0 \
        con-name "${HOTSPOT_CON}" \
        autoconnect no \
        ssid "${HOTSPOT_SSID}" \
        mode ap \
        802-11-wireless.band bg \
        802-11-wireless.channel 6 \
        802-11-wireless-security.key-mgmt wpa-psk \
        802-11-wireless-security.psk "${HOTSPOT_PASS}" \
        ipv4.method shared \
        ipv4.addresses 10.42.0.1/24 \
        ipv6.method ignore \
        connection.autoconnect-priority 1
fi

# 2. Elevate autoconnect-priority for known client Wi-Fi networks
echo "[2/4] Setting high autoconnect-priority (100) on client Wi-Fi connections..."
CLIENT_CON_LIST=$(nmcli -t -f NAME,TYPE connection show | awk -F: -v hot="${HOTSPOT_CON}" '$2 == "802-11-wireless" && $1 != hot {print $1}')

while IFS= read -r con; do
    [[ -z "$con" ]] && continue
    echo "  -> Prioritizing client connection: $con"
    nmcli connection modify "$con" connection.autoconnect yes connection.autoconnect-priority 100 || true
done <<< "$CLIENT_CON_LIST"

# 3. Install Watchdog Daemon & Sudoers Rule
echo "[3/4] Installing watchdog script and systemd service..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 755 "${SCRIPT_DIR}/wifi-failover.sh" /usr/local/bin/wifi-failover.sh
install -m 644 "${SCRIPT_DIR}/wifi-failover.service" /etc/systemd/system/wifi-failover.service

if [[ -f "${SCRIPT_DIR}/010_dvm_nexus_nmcli" ]]; then
    install -m 440 "${SCRIPT_DIR}/010_dvm_nexus_nmcli" /etc/sudoers.d/010_dvm_nexus_nmcli
fi

# 4. Enable and start watchdog service
echo "[4/4] Enabling and starting wifi-failover.service..."
systemctl daemon-reload
systemctl enable wifi-failover.service
systemctl restart wifi-failover.service

echo "=========================================================="
echo "  Field Hotspot Setup Complete!"
echo "  - Hotspot SSID: ${HOTSPOT_SSID}"
echo "  - Hotspot Password: ${HOTSPOT_PASS}"
echo "  - Fallback IP: 10.42.0.1"
echo "  - Watchdog Status: $(systemctl is-active wifi-failover.service)"
echo "=========================================================="
