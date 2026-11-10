#!/usr/bin/env bash
# ==============================================================================
# DVM Nexus - Raspberry Pi 5 Automated Installer
# Installs DVM Nexus Web Management Interface and registers systemd service.
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}        DVM Nexus - Raspberry Pi 5 Installer         ${NC}"
echo -e "${BLUE}======================================================${NC}"

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}[ERROR] This script must be run as root (use sudo).${NC}"
   exit 1
fi

INSTALL_DIR="/opt/dvm-nexus"
DVM_DIR="/opt/dvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo -e "${GREEN}[*] Source directory: ${SCRIPT_DIR}${NC}"
echo -e "${GREEN}[*] Target directory: ${INSTALL_DIR}${NC}"

# Step 1: Update package lists and install system requirements
echo -e "${YELLOW}[*] Installing system dependencies via apt...${NC}"
apt-get update -q
apt-get install -y -q python3 python3-pip python3-venv sqlite3 curl

# Step 2: Create DVM directories if they do not exist
mkdir -p "${DVM_DIR}"
mkdir -p "${INSTALL_DIR}"

# Step 3: Copy Nexus source code to /opt/dvm-nexus
echo -e "${YELLOW}[*] Deploying application files to ${INSTALL_DIR}...${NC}"
cp -r "${SCRIPT_DIR}/dvm_nexus" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/systemd" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/scripts" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/run_nexus.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"

# Step 4: Setup Python virtual environment
echo -e "${YELLOW}[*] Setting up Python virtual environment...${NC}"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

# Step 5: Initialize SQLite database and default profiles
echo -e "${YELLOW}[*] Initializing DVM Nexus database and profiles...${NC}"
cd "${INSTALL_DIR}"
"${INSTALL_DIR}/venv/bin/python" -c "
from dvm_nexus.core.database import init_db
from dvm_nexus.config_engine.profile_manager import ProfileManager
init_db()
pm = ProfileManager()
print('Seeded profiles:', [p['id'] for p in pm.list_profiles()])
"

# Step 6: Install and activate systemd service
echo -e "${YELLOW}[*] Registering systemd service...${NC}"
cp "${INSTALL_DIR}/systemd/dvm-nexus.service" /etc/systemd/system/dvm-nexus.service
systemctl daemon-reload
systemctl enable dvm-nexus.service
systemctl restart dvm-nexus.service

# Step 7: Verification
sleep 2
if systemctl is-active --quiet dvm-nexus.service; then
    PRIMARY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    echo -e "${BLUE}======================================================${NC}"
    echo -e "${GREEN}[SUCCESS] DVM Nexus is active and running!${NC}"
    echo -e "${GREEN}Access the WebUI at:${NC}"
    echo -e "   ${BLUE}http://${PRIMARY_IP}:8080${NC}  or  ${BLUE}http://localhost:8080${NC}"
    echo -e ""
    echo -e "Helpful commands:"
    echo -e " - Status:  sudo systemctl status dvm-nexus"
    echo -e " - Logs:    sudo journalctl -u dvm-nexus -f"
    echo -e " - Restart: sudo systemctl restart dvm-nexus"
    echo -e "${BLUE}======================================================${NC}"
else
    echo -e "${RED}[WARNING] dvm-nexus.service failed to start. Check logs with:${NC}"
    echo -e "   journalctl -u dvm-nexus -e"
fi
