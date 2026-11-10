# DVM Nexus

[![Version](https://img.shields.io/badge/version-1.0.13-blue.svg)](https://github.com/MrRedNeckKYN/DVM-Nexus/releases)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com)
[![Hardware](https://img.shields.io/badge/hardware-Raspberry%20Pi%205-red.svg)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Modem](https://img.shields.io/badge/modem-MMDVM%20Duplex%20Hat-blueviolet.svg)](#hardware-setup)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**DVM Nexus** is a web management interface and configuration manager for [DVMHost](https://github.com/DVMProject/dvmhost). 

Instead of SSHing into your Raspberry Pi to edit YAML configs, manually calculating P25 frequency tables, or getting locked out when your Wi-Fi changes in the field, DVM Nexus gives you a router-grade web interface to control your repeater, switch modes, monitor hardware, and manage connections from any phone, tablet, or PC.

---

## What It Does

### 📱 Modern Dashboard for Desktop & Mobile
- **Live Hardware Stats:** Real-time SoC temperature in °F, CPU load, RAM usage, storage space, and uptime.
- **Service Controls:** Start, stop, or restart `dvmhost` with one click and see live journalctl logs.
- **Phone Friendly:** Works seamlessly on mobile with a slide-out drawer, quick bottom navigation tabs, and touch-friendly controls so you can manage your station on the go.
- **Live Event Feed:** Streams repeater activity, peer connection events, and radio traffic in real time without refreshing the page.

### 📶 Automatic Failover Hotspot (Field-AP)
- **Never get locked out:** If your home Wi-Fi drops or you take your hotspot to a field day event, the Pi automatically starts its own Wi-Fi hotspot (`MMDVM-Field-AP` at `10.42.0.1`).
- **Scan & Connect on your phone:** Connect to the hotspot from your phone, open the web UI, scan for available local Wi-Fi networks, and connect with a single tap.
- **Auto-reconnect home:** When you bring your gear back home and your saved Wi-Fi comes back in range, the hotspot shuts off and reconnects automatically.
- **100% Web UI Driven:** No bash scripts, terminal commands, or SSH required. You can customize the hotspot name, password, Wi-Fi channel, and disconnect timers directly from the **Wi-Fi & Hotspot** page.

### 🔄 1-Click Radio Profile Switching
- **Hardware Protection:** An MMDVM Duplex hat cannot decode DMR and P25 simultaneously due to shared hardware registers. DVM Nexus manages this cleanly.
- **Safe Mode Switching:** Switch between saved profiles (e.g., *DMR Duplex* and *P25 Phase 1*). Nexus creates a safety snapshot, releases the serial port (`/dev/ttyAMA0`), applies the new configuration, restarts the service, and verifies it came up cleanly. If anything fails, it automatically rolls back.

### 🛠️ Network Builder Wizard
- Guided 7-step setup for new repeater deployments.
- **Automatic IDEN Bandplan Generator:** Computes APCO P25 base frequencies, TX offsets, and channel spacings (12.5 kHz) for 700MHz, 800MHz, and UHF bands.
- **Talkgroups & Access Control:** Manage talkgroup routing rules (`talkgroup_rules.yml`) and authorized Radio IDs (`rid_acl.dat`) without touching raw text files.
- **Visual Diff:** Review line-by-line configuration changes before saving.

### 🌐 Meshtastic LoRa Integration
- Connect any Meshtastic node via USB serial (`/dev/ttyUSB0`) or network/TCP (`port 4403`).
- **Two-Way Text Gateway:** Send and receive text messages between DMR/P25 handheld radios (TMS/SMS) and Meshtastic LoRa channels.
- **Mesh Messenger & Node List:** Chat directly on mesh channels and view discovered nodes, signal quality (SNR), battery levels, and GPS positions.
- **Remote Mesh Commands:** Operators can send secure commands over the mesh (`!status`, `!restart`, `!profile <name>`) to manage the repeater when out of cell/internet range.

### 🚀 1-Click In-Place Updates
- Check for updates and install new releases of DVM Nexus and DVMHost directly from the **System Updates** page.
- Backs up your settings, pulls the update, runs any migrations, and restarts the service automatically.

---

## Hardware Setup

DVM Nexus is designed for a **Raspberry Pi 5** paired with an **AURSINC MMDVM Duplex Hotspot Hat** (or any compatible STM32 ADF7021 duplex board):

- **Modem:** `STM32_HS_GPIO` (dual ADF7021 transceivers for full-duplex DMR TS1/TS2 and P25 FDMA).
- **Oscillator (TCXO):** 14.7456 MHz.
- **Port:** Raspberry Pi 40-pin GPIO UART (`/dev/ttyAMA0` @ 115200 bps).

### GPIO Pin Connections

| Pin | Signal | Pi 5 Pin | Hat Pin | Description |
|:---:|:---:|:---:|:---:|:---|
| **01** | `3.3V` | Pin 1 | 3.3V | Powers STM32 logic |
| **02** | `5.0V` | Pin 2 | 5V | Powers ADF7021 RF stages |
| **06** | `GND` | Pin 6 | GND | Ground reference |
| **08** | `TXD` | GPIO 14 (Pin 8) | RXD | Pi sends data to modem |
| **10** | `RXD` | GPIO 15 (Pin 10) | TXD | Modem sends data to Pi |

---

## Installation on Raspberry Pi

On a fresh install of Raspberry Pi OS (Debian 12 Bookworm):

```bash
curl -sSL https://raw.githubusercontent.com/MrRedNeckKYN/DVM-Nexus/main/install.sh | sudo bash
```

Or from a cloned repository:

```bash
git clone https://github.com/MrRedNeckKYN/DVM-Nexus.git
cd DVM-Nexus
sudo ./install.sh
```

### What gets installed:
1. Configures the Pi 5 GPIO serial port (`/dev/ttyAMA0`) and disables the Linux serial console.
2. Sets up DVM Nexus in `/opt/dvm-nexus` with an isolated Python environment.
3. Initializes the database and sets up default DMR and P25 profiles.
4. Automatically provisions the failover hotspot profile (`MMDVM-Field-AP`).
5. Enables and starts the `dvm-nexus` systemd service on boot.

Once installed, open your browser from any device on your local network:
```text
http://<your-pi-ip>:8080
```

---

## Local Development & Testing

DVM Nexus includes built-in mock drivers for hardware telemetry, Wi-Fi networking, and radio daemons, so you can develop and test on Windows, macOS, or Linux without needing a physical Pi or MMDVM hat attached.

```bash
# 1. Clone repository
git clone https://github.com/MrRedNeckKYN/DVM-Nexus.git
cd DVM-Nexus

# 2. Create virtual environment
python3 -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows PowerShell:
.\venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run automated tests
python -m pytest tests -v

# 5. Start dev server
python run_nexus.py --host 0.0.0.0 --port 8080 --reload
```

---

## Common Commands & File Paths

### Helpful Service Commands

```bash
# Web UI service status & logs
sudo systemctl status dvm-nexus
sudo journalctl -u dvm-nexus -f

# DVMHost radio daemon status & logs
sudo systemctl status dvmhost
sudo journalctl -u dvmhost -f
```

### Important File Locations

- **Active DVMHost Config:** `/opt/dvm/config.yml`
- **IDEN Bandplan Table:** `/opt/dvm/iden_table.dat`
- **Talkgroup Rules:** `/opt/dvm/talkgroup_rules.yml`
- **Radio ID ACL:** `/opt/dvm/rid_acl.dat`
- **Nexus Database:** `/opt/dvm-nexus/dvm_nexus/data/nexus.db`
- **Hotspot Settings:** `/opt/dvm-nexus/dvm_nexus/data/hotspot_config.json`
- **Config Backups:** `/opt/dvm-nexus/dvm_nexus/data/backups/`
- **Radio Profiles:** `/opt/dvm-nexus/dvm_nexus/data/profiles/`

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
