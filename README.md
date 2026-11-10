# DVM Nexus

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hardware: RPi 5](https://img.shields.io/badge/Hardware-Raspberry%20Pi%205-red.svg)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Modem: MMDVM Duplex](https://img.shields.io/badge/Modem-AURSINC%20Duplex%20Hat-blueviolet.svg)](#hardware-architecture)

**DVM Nexus** is a modernized, router-grade web management interface and configuration engine designed specifically for the [DVMProject](https://github.com/DVMProject) ecosystem centered around `dvmhost`.

Instead of manually editing YAML configuration files, managing IDEN frequency calculation tables by hand, or risking broken network deployments in the terminal, DVM Nexus provides a guided, web-first administrative experience inspired by modern enterprise network routers.

---

## Key Features

### 1. Unified Router-Grade Dashboard
- **Hardware Telemetry:** Real-time monitoring of Raspberry Pi 5 SoC temperature (`/sys/class/thermal`), CPU load, RAM usage, storage utilization, and system uptime.
- **DVMHost Service Control:** 1-click Start, Stop, and Restart with post-start verification polling, journalctl log inspection, and simulated fallbacks during offline development.
- **Live Activity Feed:** Server-Sent Events (SSE) stream providing real-time peer connection events, protocol states, and diagnostic messages without client polling.

### 2. Guided Network Builder (Wizard)
- 7-step guided configuration wizard for deploying new DMR or P25 networks from scratch:
  1. **Protocol Selection:** Choose between DMR Duplex or P25 Phase 1 Conventional / Repeater.
  2. **System Identifiers:** Configure DMR Color Code (0–15), Network ID, or P25 NAC (`$293`), System ID (`$001`), and WACN (`$BB800`).
  3. **IDEN Bandplan Calculator:** Built-in APCO P25 bandplan generator (`iden_table.dat`) computing Base Frequency, TX Offset, Channel Spacing (12.5 kHz), and Channel Numbers for 700MHz, 800MHz, and UHF bands.
  4. **Talkgroup Rules:** Provision group voice talkgroups, timeslots (TS1 / TS2), and network routing rules (`talkgroup_rules.yml`).
  5. **Radio ID Access Control (ACL):** Maintain authorized subscriber lists (`rid_acl.dat`) with permit/deny flags.
  6. **Visual Diff Inspection:** Review exact line-by-line configuration diffs before committing any changes to disk.
  7. **Atomic Apply & Verify:** Applies changes safely with automated rollback if the service fails to verify.

### 3. Radio Profile Management & Modem Mutual Exclusion
- **Shared Hardware Resource Management:** A single MMDVM Duplex modem (dual ADF7021 transceivers) cannot decode DMR and P25 simultaneously due to shared STM32 DSP filters and baseband registers.
- **1-Click Profile Switching:** Seamlessly switch between saved profiles (e.g., *Default DMR Duplex* and *Default P25 Phase 1*).
- **Zero-Corruption Switching Pipeline:**
  1. Validates destination profile configuration.
  2. Creates a timestamped snapshot of the active configuration.
  3. Safely shuts down `dvmhost.service` to release `/dev/ttyAMA0`.
  4. Atomically replaces `config.yml`, `iden_table.dat`, `talkgroup_rules.yml`, and `rid_acl.dat`.
  5. Starts `dvmhost.service`.
  6. Polls service health; automatically restores previous files and restarts if the new profile crashes.

### 4. Protocol-Specific Management Screens
- **DMR Management:** Configure Color Codes, callhang timers, timeslot mapping, talkgroups, peer connections, and issue over-the-air commands (Radio Check, Radio Inhibit, Radio Uninhibit, Alert Page).
- **P25 Phase 1 Management:** Configure NAC, WACN, System ID, Site ID, low-speed data, talkgroup rules, IDEN bandplan tables, and over-the-air subscriber commands.
- **Modem & ADF7021 Transceiver Tuning:** Adjust UART baud rates (115200 bps), serial ports (`/dev/ttyAMA0`), TX/RX inversion, frequency correction offsets (`rxFreqCorrection`, `txFreqCorrection`), and filter bandwidths.

### 5. Robust Configuration Management
- **Atomic Writing:** Uses `tempfile.mkstemp` and `os.replace` to prevent truncated or corrupt files during system crashes or power outages.
- **Snapshot History:** Every modification creates a timestamped snapshot in `data/backups/`.
- **1-Click Rollback:** Restore any prior snapshot with one click directly from the WebUI.
- **Thread-Safe DVMHost REST API Client:** Synchronizes all REST API calls through an SHA-256 token mutex condition variable, respecting DVMHost's strict single-active-token-per-IP limit.

### 6. Advanced Transport-Independent Meshtastic Integration
- **Transport Agnostic:** Supports any Meshtastic device architecture:
  - **USB Serial:** Direct hardware connection via `/dev/ttyUSB0`, `/dev/ttyACM0`, or `COM` ports with auto-port discovery.
  - **TCP / Network:** Connect to WiFi-enabled nodes (Heltec, T-Beam, T-Echo) over standard port `4403`.
  - **BLE:** Bluetooth Low Energy connection and device discovery.
  - **Simulated Mesh:** Virtual multi-node simulator enabling immediate UI testing, bridge evaluation, and development without physical hardware.
- **Live Mesh Messenger:** Interactive chat panel with real-time RX/TX activity, channel selection (Ch 0–7), direct messaging to discovered nodes, and broadcast messaging.
- **Node Directory & Telemetry:** Discovered nodes table displaying Node ID, Long/Short Name, Hardware Model, Battery Level (%), Battery Voltage, SNR (dB), GPS coordinates, and Last Heard timestamps.
- **Bidirectional Radio <-> Meshtastic Text Gateway:** True two-way SMS / TMS bridging:
  - **Radio ➔ Meshtastic:** Over-the-air text messages (DMR TMS or P25 PDU) sent from handheld radios are automatically converted and transmitted onto the designated Meshtastic channel.
  - **Meshtastic ➔ Radio:** Messages typed on Meshtastic handsets or phones (or prefixed with `#dmr <text>` / `#p25 <text>`) are translated and broadcast over-the-air to the target DMR or P25 Talkgroup.
  - **Web Dispatch:** Transmit SMS directly from the DMR, P25, or Meshtastic web panels with simultaneous mirroring to both networks.
- **DMR / P25 to Meshtastic Event Bridge:** Configurable routing engine that triggers Meshtastic notifications on DMR/P25 talkgroup activity, repeater state transitions, and radio profile switches.
- **Guarded Remote Management:** Authorized operators can manage DVMHost over the mesh with strict security:
  - Whitelisted commands only (`!status`, `!modem`, `!profiles`, `!restart`, `!profile <name>`).
  - No arbitrary shell execution.
  - Two-step confirmation with 60-second expiration for disruptive actions (service restart, profile switch).
  - Anti-replay protection and 5 cmd/min rate limiting.

---

## Hardware Architecture & Raspberry Pi 5 Pinout

DVM Nexus is optimized for the **AURSINC MMDVM Duplex Hotspot Module Dual Hat** mounted on a **Raspberry Pi 5**:

- **Transceivers:** Dual Analog Devices ADF7021 on-board transceivers (enabling full duplex transmit and receive on DMR TS1/TS2 or P25 FDMA).
- **Oscillator (TCXO):** 14.7456 MHz (`ECS-TXO-3225-147.4-TR`).
- **Modem Selection:** `STM32_HS_GPIO`.
- **Interface:** Raspberry Pi 40-Pin GPIO Header UART (`/dev/ttyAMA0`).

### GPIO Header Pinout

| Pin # | Signal Name | Raspberry Pi 5 Header | MMDVM Hat Header | Notes |
|:---:|:---:|:---:|:---:|:---|
| **01** | `3.3V DC` | Physical Pin 1 | 3.3V Power | Powers STM32 logic |
| **02** | `5.0V DC` | Physical Pin 2 | 5V Power | Powers ADF7021 RF stages |
| **06** | `GND` | Physical Pin 6 | Ground | Common system ground |
| **08** | `TXD` | GPIO 14 (Physical 8) | Modem RXD | Pi transmits commands to Modem |
| **10** | `RXD` | GPIO 15 (Physical 10) | Modem TXD | Modem sends packets to Pi |

---

## Quickstart (Local Development & Testing)

DVM Nexus includes fallback mock drivers for hardware telemetry and service controllers, allowing full development and testing on any operating system (Linux, macOS, Windows).

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/MrRedNeckKYN/DVM-Nexus.git
cd DVM-Nexus

python3 -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows:
.\venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Run Integration Tests

```bash
python -m pytest tests -v
```

All 27 automated integration tests validate IDEN calculation, YAML configuration reading/writing, atomic backups, profile switching, Meshtastic text bridging, and system auto-updating.

### 3. Launch Development Server

```bash
python run_nexus.py --host 0.0.0.0 --port 8080 --reload
```

Open your browser and navigate to `http://localhost:8080`.

---

## Raspberry Pi 5 Auto-Installer (One-Liner)

On a fresh **Raspberry Pi 5** running Raspberry Pi OS (Debian 12 Bookworm), run the automated installer:

```bash
curl -sSL https://raw.githubusercontent.com/MrRedNeckKYN/DVM-Nexus/main/install.sh | sudo bash
```

Alternatively, if you have already cloned the repository:

```bash
sudo ./install.sh
```

### What the Auto-Installer Does Automatically:
1. **Configures Raspberry Pi 5 GPIO UART (`/dev/ttyAMA0`):**
   - Applies device tree overlays in `/boot/firmware/config.txt` (`enable_uart=1`, `dtoverlay=uart0`, `dtoverlay=disable-bt`).
   - Disables and masks Linux serial console on `ttyAMA0`.
   - Strips serial console parameters from `/boot/firmware/cmdline.txt`.
   - Grants user permissions in `dialout` and `gpio` groups.
2. **Installs System Dependencies:** Installs `python3`, `pip`, `venv`, `git`, `sqlite3`, `curl`, and `socat`.
3. **Deploys Application & Virtual Environment:** Installs DVM Nexus to `/opt/dvm-nexus` with isolated Python virtualenv.
4. **Initializes Database & Profiles:** Automatically seeds factory DMR Duplex and P25 Phase 1 profiles, Meshtastic settings, and bidirectional text routes.
5. **Registers Systemd Service:** Installs and enables `dvm-nexus.service` to start on boot.
6. **Installs CLI Management Utility:** Provides `/usr/local/bin/dvm-nexus` for quick command-line administration.

---

## System Auto-Updater (Web UI & CLI)

DVM Nexus features an integrated in-place auto-updater:

### 1. Web UI Auto-Updater
- Navigate to **System Updates** (`http://<ip>:8080/updates`) in the sidebar.
- Click **"Check for Updates"** to query upstream releases and commit logs.
- Click **"Install Update Now (1-Click)"**:
  - Automatically takes a pre-update safety configuration snapshot.
  - Pulls latest upstream code from Git.
  - Upgrades Python dependencies from `requirements.txt`.
  - Runs database migrations.
  - Restarts the `dvm-nexus.service` daemon cleanly.

### 2. Command-Line Updating via SSH
```bash
# Check and apply updates in-place
sudo dvm-nexus update

# View live system logs
dvm-nexus logs

# Check service status
dvm-nexus status
```
sudo ./scripts/setup-pi5-uart.sh
```

This script:
1. Backs up `/boot/firmware/config.txt` and `/boot/firmware/cmdline.txt`.
2. Disables and masks `serial-getty@ttyAMA0.service`.
3. Strips serial console parameters from `cmdline.txt`.
4. Enables `enable_uart=1`, `dtoverlay=uart0`, and `dtoverlay=disable-bt`.
5. Adds your user account to the `dialout` and `gpio` groups.
6. Prompts for a reboot.

### Step 2: Install DVM Nexus & Systemd Service

After rebooting, run the automated installer:

```bash
sudo chmod +x scripts/install-nexus.sh
sudo ./scripts/install-nexus.sh
```

The installer:
- Deploys application files to `/opt/dvm-nexus`.
- Creates a dedicated Python virtual environment in `/opt/dvm-nexus/venv`.
- Initializes the SQLite database and seeds default DMR and P25 profiles.
- Installs `/etc/systemd/system/dvm-nexus.service`.
- Enables and starts the service automatically on boot.

### Step 3: Access the Management WebUI

Open your browser from any device on your local network:
```text
http://<raspberry-pi-ip>:8080
```

---

## System Architecture

```text
+---------------------------------------------------------------+
|                      DVM Nexus Web UI                         |
|   (HTMX + Jinja2 Templates + Dark Router-Grade CSS Styles)   |
+-------------------------------+-------------------------------+
                                | HTTP / SSE
                                v
+---------------------------------------------------------------+
|                     FastAPI Application                       |
|   - Dashboard Router          - DMR Management Router         |
|   - Network Builder Wizard    - P25 Management Router         |
|   - Profile Switcher Router   - Modem & Transceiver Router    |
|   - Config History / Diff     - JSON API & SSE Stream         |
+-------------------------------+-------------------------------+
                                |
        +-----------------------+-----------------------+
        v                                               v
+-------------------------------+       +-------------------------------+
|     DVM Nexus Config Engine   |       |         Nexus Core            |
| - IDEN Calculator             |       | - System Telemetry (SoC Temp) |
| - Network Schema Validator    |       | - Service Controller (systemd)|
| - Atomic Backup Manager       |       | - DVMHost REST API Client     |
| - Line-by-Line Diff Engine    |       | - SQLite Revision DB          |
| - Profile Manager             |       |                               |
+---------------+---------------+       +---------------+---------------+
                |                                       |
                v                                       v
+-------------------------------+       +-------------------------------+
|  Active DVM Files (/opt/dvm)  |       |        DVMHost Service        |
| - config.yml                  | <---> | - REST API (Port 9990)        |
| - iden_table.dat              |       | - Serial UART (/dev/ttyAMA0)  |
| - talkgroup_rules.yml         |       +---------------+---------------+
| - rid_acl.dat                 |                       |
+-------------------------------+                       v
                                        +-------------------------------+
                                        |   AURSINC MMDVM Duplex Hat    |
                                        | - STM32_HS_GPIO Modem         |
                                        | - Dual ADF7021 Transceivers   |
                                        | - 14.7456 MHz TCXO            |
                                        +-------------------------------+
```

---

## Service Management & Troubleshooting

### Systemd Commands

```bash
# Check DVM Nexus status
sudo systemctl status dvm-nexus

# Follow live DVM Nexus logs
sudo journalctl -u dvm-nexus -f

# Check DVMHost radio daemon status
sudo systemctl status dvmhost

# Follow live DVMHost logs
sudo journalctl -u dvmhost -f
```

### Configuration File Locations

- **Active DVMHost Config:** `/opt/dvm/config.yml`
- **IDEN Bandplan Table:** `/opt/dvm/iden_table.dat`
- **Talkgroup Rules:** `/opt/dvm/talkgroup_rules.yml`
- **Radio ID ACL:** `/opt/dvm/rid_acl.dat`
- **Nexus SQLite Database:** `/opt/dvm-nexus/dvm_nexus/data/nexus.db`
- **Configuration Backups:** `/opt/dvm-nexus/dvm_nexus/data/backups/`
- **Saved Radio Profiles:** `/opt/dvm-nexus/dvm_nexus/data/profiles/`

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
