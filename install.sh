#!/usr/bin/env bash
# ==============================================================================
# DVM Nexus - Unified Raspberry Pi 5 Auto-Installer
# Optimized for Raspberry Pi 5 running Debian 12 (Bookworm) and MMDVM Duplex Hat.
# Can be run directly via:
#   curl -sSL https://raw.githubusercontent.com/MrRedNeckKYN/DVM-Nexus/main/install.sh | sudo bash
# Or from a cloned repository:
#   sudo ./install.sh
# ==============================================================================

set -euo pipefail

# ANSI Color Codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

REPO_URL="https://github.com/MrRedNeckKYN/DVM-Nexus.git"
INSTALL_DIR="/opt/dvm-nexus"
DVM_DIR="/opt/dvm"
BRANCH="main"

echo -e "${CYAN}${BOLD}"
echo "  ██████╗ ██╗   ██╗███╗   ███╗    ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗"
echo "  ██╔══██╗██║   ██║████╗ ████║    ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝"
echo "  ██║  ██║██║   ██║██╔████╔██║    ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗"
echo "  ██║  ██║╚██╗ ██╔╝██║╚██╔╝██║    ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║"
echo "  ██████╔╝ ╚████╔╝ ██║ ╚═╝ ██║    ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║"
echo "  ╚═════╝   ╚═══╝  ╚═╝     ╚═╝    ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
echo -e "${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "${BOLD}       DVM Nexus - Raspberry Pi 5 Auto-Installer & System Setup       ${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 1. Root Verification
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}[ERROR] This installer must be run as root (e.g. sudo bash install.sh).${NC}"
   exit 1
fi

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo 'pi')}"
echo -e "${GREEN}[*] Target operating user: ${BOLD}${TARGET_USER}${NC}"

# 2. Hardware & OS Detection
echo -e "${YELLOW}[*] Performing pre-flight hardware and OS checks...${NC}"
IS_PI5=false
if [[ -f "/proc/device-tree/model" ]]; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model)
    echo -e "${GREEN}[*] Detected hardware: ${BOLD}${MODEL}${NC}"
    if [[ "$MODEL" =~ "Raspberry Pi 5" ]]; then
        IS_PI5=true
    fi
fi

# Locate boot firmware directory (Debian Bookworm uses /boot/firmware)
if [[ -d "/boot/firmware" ]]; then
    FW_DIR="/boot/firmware"
elif [[ -d "/boot" ]]; then
    FW_DIR="/boot"
else
    FW_DIR="/boot/firmware"
    mkdir -p "${FW_DIR}"
fi

CONFIG_TXT="${FW_DIR}/config.txt"
CMDLINE_TXT="${FW_DIR}/cmdline.txt"
echo -e "${GREEN}[*] Firmware boot path: ${FW_DIR}${NC}"

# 3. System Packages Installation
echo -e "${YELLOW}[*] Updating apt package cache and installing dependencies...${NC}"
apt-get update -q
apt-get install -y -q \
    python3 \
    python3-pip \
    python3-venv \
    git \
    sqlite3 \
    curl \
    socat \
    logrotate

# 4. Raspberry Pi 5 GPIO UART Configuration for AURSINC MMDVM Duplex Hat
echo -e "${YELLOW}[*] Configuring GPIO UART (/dev/ttyAMA0) for MMDVM Duplex Hat...${NC}"

# Backup boot configs
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
if [[ -f "${CONFIG_TXT}" ]]; then
    cp "${CONFIG_TXT}" "${CONFIG_TXT}.bak_${TIMESTAMP}"
fi
if [[ -f "${CMDLINE_TXT}" ]]; then
    cp "${CMDLINE_TXT}" "${CMDLINE_TXT}.bak_${TIMESTAMP}"
fi

# Stop and mask Linux serial console on ttyAMA0
systemctl stop serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl disable serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true

# Strip console=serial0 or console=ttyAMA0 from cmdline.txt
if [[ -f "${CMDLINE_TXT}" ]]; then
    sed -i -E 's/console=(serial0|ttyAMA0|tty1),[0-9]+ //g' "${CMDLINE_TXT}"
    sed -i -E 's/console=(serial0|ttyAMA0),[0-9]+//g' "${CMDLINE_TXT}"
fi

# Configure config.txt for primary UART and Bluetooth detachment
if [[ -f "${CONFIG_TXT}" ]]; then
    # enable_uart=1
    if grep -q "^enable_uart=" "${CONFIG_TXT}"; then
        sed -i 's/^enable_uart=.*/enable_uart=1/' "${CONFIG_TXT}"
    else
        echo "enable_uart=1" >> "${CONFIG_TXT}"
    fi

    # dtoverlay=uart0
    if ! grep -q "^dtoverlay=uart0" "${CONFIG_TXT}"; then
        echo "dtoverlay=uart0" >> "${CONFIG_TXT}"
    fi

    # dtoverlay=disable-bt
    if ! grep -q "^dtoverlay=disable-bt" "${CONFIG_TXT}"; then
        echo "# Dedicated UART for MMDVM Duplex Hat" >> "${CONFIG_TXT}"
        echo "dtoverlay=disable-bt" >> "${CONFIG_TXT}"
    fi
fi

# Disable hciuart service
systemctl disable hciuart.service 2>/dev/null || true

# Grant user dialout and gpio permissions
if id "${TARGET_USER}" &>/dev/null; then
    usermod -a -G dialout,gpio "${TARGET_USER}"
    echo -e "${GREEN}[+] User '${TARGET_USER}' added to 'dialout' and 'gpio' groups.${NC}"
fi

# 5. DVMHost Directory Preparation
echo -e "${YELLOW}[*] Preparing /opt/dvm directory...${NC}"
mkdir -p "${DVM_DIR}"
chown -R "${TARGET_USER}:${TARGET_USER}" "${DVM_DIR}" 2>/dev/null || true

# 6. Deploy DVM Nexus Application
echo -e "${YELLOW}[*] Deploying DVM Nexus to ${INSTALL_DIR}...${NC}"
mkdir -p "${INSTALL_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/run_nexus.py" ]]; then
    # Running from existing cloned repository
    echo -e "${GREEN}[*] Copying from local source: ${SCRIPT_DIR}${NC}"
    cp -r "${SCRIPT_DIR}/dvm_nexus" "${INSTALL_DIR}/"
    cp -r "${SCRIPT_DIR}/systemd" "${INSTALL_DIR}/"
    cp -r "${SCRIPT_DIR}/scripts" "${INSTALL_DIR}/"
    cp "${SCRIPT_DIR}/run_nexus.py" "${INSTALL_DIR}/"
    cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
    if [[ -d "${SCRIPT_DIR}/.git" ]]; then
        cp -r "${SCRIPT_DIR}/.git" "${INSTALL_DIR}/"
    fi
else
    # Running via curl | bash: clone repository from GitHub
    echo -e "${GREEN}[*] Cloning repository from GitHub: ${REPO_URL} (${BRANCH})...${NC}"
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        git -C "${INSTALL_DIR}" fetch origin "${BRANCH}"
        git -C "${INSTALL_DIR}" reset --hard "origin/${BRANCH}"
    else
        git clone -b "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
    fi
fi

# 7. Python Virtual Environment & Dependencies
echo -e "${YELLOW}[*] Setting up Python virtual environment...${NC}"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

# 8. Initialize SQLite Database & Seed Profiles
echo -e "${YELLOW}[*] Initializing database and radio profiles...${NC}"
cd "${INSTALL_DIR}"
"${INSTALL_DIR}/venv/bin/python" -c "
from dvm_nexus.core.database import init_db
from dvm_nexus.config_engine.profile_manager import ProfileManager
init_db()
pm = ProfileManager()
print('Initialized radio profiles:', [p['name'] for p in pm.list_profiles()])
"

# Set ownership and Git safe.directory exception (prevents dubious ownership errors)
chown -R "${TARGET_USER}:${TARGET_USER}" "${INSTALL_DIR}" 2>/dev/null || true
git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null || true
if [[ -n "${TARGET_USER}" ]]; then
    su - "${TARGET_USER}" -c "git config --global --add safe.directory '${INSTALL_DIR}'" 2>/dev/null || true
fi

# 9. Register Systemd Service
echo -e "${YELLOW}[*] Installing systemd service...${NC}"
cp "${INSTALL_DIR}/systemd/dvm-nexus.service" /etc/systemd/system/dvm-nexus.service
systemctl daemon-reload
systemctl enable dvm-nexus.service
systemctl restart dvm-nexus.service

# 10. Install CLI Management Tool (/usr/local/bin/dvm-nexus)
echo -e "${YELLOW}[*] Installing 'dvm-nexus' CLI tool...${NC}"
cat << 'EOF' > /usr/local/bin/dvm-nexus
#!/usr/bin/env bash
# DVM Nexus CLI Management Wrapper
INSTALL_DIR="/opt/dvm-nexus"

case "${1:-status}" in
    status)
        systemctl status dvm-nexus.service
        ;;
    start)
        sudo systemctl start dvm-nexus.service
        ;;
    stop)
        sudo systemctl stop dvm-nexus.service
        ;;
    restart)
        sudo systemctl restart dvm-nexus.service
        ;;
    logs)
        journalctl -u dvm-nexus.service -f
        ;;
    update)
        echo "Checking and applying DVM Nexus updates..."
        cd "$INSTALL_DIR"
        sudo -u root "$INSTALL_DIR/venv/bin/python" -c "
from dvm_nexus.core.updater import system_updater
res = system_updater.apply_update()
print(res.get('message', res))
"
        ;;
    *)
        echo "Usage: dvm-nexus {status|start|stop|restart|logs|update}"
        exit 1
        ;;
esac
EOF
chmod +x /usr/local/bin/dvm-nexus

# 11. Log Rotation
cat << 'EOF' > /etc/logrotate.d/dvm-nexus
/opt/dvm-nexus/dvm_nexus/data/*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
EOF

# 12. Completion & Verification
sleep 2
PRIMARY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo -e "${BLUE}======================================================================${NC}"
if systemctl is-active --quiet dvm-nexus.service; then
    echo -e "${GREEN}${BOLD}[SUCCESS] DVM Nexus has been successfully installed and started!${NC}"
else
    echo -e "${YELLOW}[NOTICE] Installation completed, but service is initializing.${NC}"
fi
echo -e "${BLUE}======================================================================${NC}"
echo -e "${BOLD}Access the DVM Nexus Web Interface:${NC}"
echo -e "   👉 ${CYAN}${BOLD}http://${PRIMARY_IP}:8080${NC}  or  ${CYAN}http://localhost:8080${NC}"
echo -e ""
echo -e "${BOLD}Helpful CLI Commands:${NC}"
echo -e "   dvm-nexus status   - Check web service status"
echo -e "   dvm-nexus restart  - Restart web service"
echo -e "   dvm-nexus logs     - Follow real-time service logs"
echo -e "   dvm-nexus update   - Check and apply updates in-place"
echo -e ""
echo -e "${YELLOW}${BOLD}IMPORTANT HARDWARE NOTE:${NC}"
echo -e "GPIO UART overlays for the AURSINC MMDVM Duplex Hat were configured."
echo -e "If this is the first installation on this Raspberry Pi, a reboot is required."
echo -e "${BLUE}======================================================================${NC}"

if [ -t 0 ]; then
    read -p "Would you like to reboot the Raspberry Pi now? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${GREEN}[*] Rebooting Raspberry Pi now...${NC}"
        reboot
    fi
fi
