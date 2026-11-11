#!/usr/bin/env bash
# ==============================================================================
# DVM Nexus - Raspberry Pi 5 UART & Hardware Modem Configuration Script
# Configures GPIO UART (/dev/ttyAMA0) for AURSINC MMDVM Duplex Hotspot Module
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   DVM Nexus - Raspberry Pi 5 GPIO UART Setup Tool   ${NC}"
echo -e "${BLUE}======================================================${NC}"

# Check for root privileges
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}[ERROR] This script must be run as root (use sudo).${NC}"
   exit 1
fi

# Detect Raspberry Pi firmware directory (Bookworm uses /boot/firmware)
if [[ -d "/boot/firmware" ]]; then
    FW_DIR="/boot/firmware"
elif [[ -d "/boot" ]]; then
    FW_DIR="/boot"
else
    echo -e "${RED}[ERROR] Cannot locate Raspberry Pi boot partition.${NC}"
    exit 1
fi

CONFIG_TXT="${FW_DIR}/config.txt"
CMDLINE_TXT="${FW_DIR}/cmdline.txt"

echo -e "${GREEN}[*] Boot directory detected: ${FW_DIR}${NC}"

# Step 1: Backup boot configuration
BACKUP_TIMESTAMP=$(date +%Y%m%d_%H%M%S)
if [[ -f "${CONFIG_TXT}" ]]; then
    cp "${CONFIG_TXT}" "${CONFIG_TXT}.bak_${BACKUP_TIMESTAMP}"
    echo -e "${GREEN}[*] Backed up ${CONFIG_TXT} to ${CONFIG_TXT}.bak_${BACKUP_TIMESTAMP}${NC}"
fi

if [[ -f "${CMDLINE_TXT}" ]]; then
    cp "${CMDLINE_TXT}" "${CMDLINE_TXT}.bak_${BACKUP_TIMESTAMP}"
    echo -e "${GREEN}[*] Backed up ${CMDLINE_TXT} to ${CMDLINE_TXT}.bak_${BACKUP_TIMESTAMP}${NC}"
fi

# Step 2: Disable Linux serial login console on ttyAMA0
echo -e "${YELLOW}[*] Disabling serial console on /dev/ttyAMA0...${NC}"
systemctl stop serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl disable serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true

# Strip console=serial0,... or console=ttyAMA0,... from cmdline.txt
if [[ -f "${CMDLINE_TXT}" ]]; then
    sed -i -E 's/console=(serial0|ttyAMA0|tty1),[0-9]+ //g' "${CMDLINE_TXT}"
    sed -i -E 's/console=(serial0|ttyAMA0),[0-9]+//g' "${CMDLINE_TXT}"
    echo -e "${GREEN}[+] Serial console removed from ${CMDLINE_TXT}${NC}"
fi

# Step 3: Configure config.txt for primary UART and Bluetooth disable
echo -e "${YELLOW}[*] Updating ${CONFIG_TXT} for MMDVM Duplex Hat UART...${NC}"

# Ensure enable_uart=1
if grep -q "^enable_uart=" "${CONFIG_TXT}"; then
    sed -i 's/^enable_uart=.*/enable_uart=1/' "${CONFIG_TXT}"
else
    echo "enable_uart=1" >> "${CONFIG_TXT}"
fi

# Ensure dtparam=uart0=on and dtoverlay=uart0 are configured for GPIO 14/15 on Pi 5
if ! grep -q "^dtparam=uart0=on" "${CONFIG_TXT}"; then
    echo "dtparam=uart0=on" >> "${CONFIG_TXT}"
fi
if ! grep -q "^dtoverlay=uart0" "${CONFIG_TXT}"; then
    echo "dtoverlay=uart0" >> "${CONFIG_TXT}"
fi

# Disable Bluetooth or detach from AMA0 to prevent interference
if ! grep -q "^dtoverlay=disable-bt" "${CONFIG_TXT}"; then
    echo "# Disable onboard Bluetooth to keep /dev/ttyAMA0 dedicated to MMDVM Duplex Hat" >> "${CONFIG_TXT}"
    echo "dtoverlay=disable-bt" >> "${CONFIG_TXT}"
fi

# Stop and disable hciuart service if disable-bt is used
systemctl disable hciuart.service 2>/dev/null || true

# Step 4: Grant dialout permissions
TARGET_USER="${SUDO_USER:-pi}"
if id "${TARGET_USER}" &>/dev/null; then
    usermod -a -G dialout,gpio "${TARGET_USER}"
    echo -e "${GREEN}[+] Added user '${TARGET_USER}' to dialout and gpio groups.${NC}"
fi
if id "dvmhost" &>/dev/null; then
    usermod -a -G dialout,gpio "dvmhost"
    echo -e "${GREEN}[+] Added user 'dvmhost' to dialout and gpio groups.${NC}"
fi

echo -e "${BLUE}======================================================${NC}"
echo -e "${GREEN}[SUCCESS] UART configuration complete!${NC}"
echo -e "${YELLOW}Hardware notes for AURSINC MMDVM Duplex Hotspot Hat:${NC}"
echo -e " - Port: /dev/ttyAMA0 (Speed: 115200)"
echo -e " - TCXO: 14.7456 MHz (Standard ECS-TXO-3225-147.4-TR)"
echo -e " - ADF7021: Dual transceivers for full-duplex operation"
echo -e " - A reboot is required for device tree / UART changes to take effect."
echo -e "${BLUE}======================================================${NC}"
read -p "Would you like to reboot the Raspberry Pi now? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}[*] Rebooting now...${NC}"
    reboot
fi
