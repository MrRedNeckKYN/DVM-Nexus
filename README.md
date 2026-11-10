# DVM Nexus

[![Version: 1.0.11](https://img.shields.io/badge/version-1.0.11-blue.svg)](https://github.com/MrRedNeckKYN/DVM-Nexus/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hardware: RPi 5](https://img.shields.io/badge/Hardware-Raspberry%20Pi%205-red.svg)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Modem: MMDVM Duplex](https://img.shields.io/badge/Modem-AURSINC%20Duplex%20Hat-blueviolet.svg)](#hardware-architecture)
[![Tests: 49 Passed](https://img.shields.io/badge/tests-49%20passed-success.svg)](#automated-testing)

**DVM Nexus** is a modernized, router-grade web management interface, telemetry dashboard, and configuration engine engineered specifically for the [DVMProject](https://github.com/DVMProject) ecosystem centered around `dvmhost`.

Instead of manually editing YAML files, calculating IDEN frequency tables by hand, or debugging disconnected Wi-Fi in the field over a serial cable, DVM Nexus provides a guided, web-first administrative experience with automatic failover Wi-Fi hotspot support, real-time telemetry HUD, and transport-independent Meshtastic bridging.

---

## Key Features

### 1. Dual-Viewport Responsive Router Dashboard
- **Desktop Viewport ($\ge 1024\text{px}$ / `lg:`):**
  - Persistent 260px fixed vertical navigation sidebar with categorized menus and live hardware status.
  - Asymmetric multi-column grid docking the Radio Status Hero and Activity Log on the left, with the Hardware Telemetry HUD and Service Controls neatly docked on the right.
- **Mobile & Tablet Viewport ($< 1024\text{px}$):**
  - Off-canvas slide-over drawer navigation with semi-transparent dark backdrop blur (`w-72 bg-slate-900/95`), accessible via hamburger button or bottom "Menu" tab, with ESC key and tap-outside dismissal.
  - Persistent bottom navigation bar with safe-area inset padding (`pb-[calc(0.5rem+env(safe-area-inset-bottom))]`) providing quick 1-touch tabs: **Dash**, **DMR**, **P25**, **Mesh**, and **Menu**.
  - Padded layout (`pb-24`) preventing the bottom bar from obscuring interactive controls.
- **Touch Ergonomics:** Enforces a minimum $44\times 44\text{px}$ touch target on all mobile buttons, links, and form inputs, with tactile press response (`active:scale-[0.98]`).
- **Live Activity Feed:** Server-Sent Events (SSE) stream delivering real-time peer connections, RF transmissions, and system diagnostics without client polling.

### 2. Automatic Failover Access Point (Field Hotspot) & Wi-Fi Manager
- **Field Hotspot (`MMDVM-Field-AP`):** Built-in failover hotspot configuration powered by NetworkManager (`nmcli`) and `dnsmasq` shared IPv4 routing (`10.42.0.1/24`).
- **Automated Watchdog Daemon (`wifi-failover.service`):**
  - Monitors client Wi-Fi association and gateway reachability every 20 seconds.
  - If disconnected from client Wi-Fi for $> 45$ seconds, automatically activates `Field-Hotspot` so you can connect from a smartphone or tablet in the field.
  - While in Hotspot mode, conducts background Wi-Fi scans. When a saved station/home Wi-Fi network comes back into range, it automatically tears down the hotspot and restores client Wi-Fi.
  - **Manual Override Locking:** Web UI toggles activate a 5-minute override flag (`/tmp/wifi-manual.override`) to ensure the watchdog doesn't interrupt intentional configuration sessions.
- **Web UI Wi-Fi Management (`/network`):**
  - Live Status Hero Card displaying active mode (Client vs. Field AP), assigned IP, gateway, signal percentage, interface (`wlan0`), and MAC address.
  - 1-Click tactile mode toggle: **Switch to Field Hotspot** / **Switch to Client Wi-Fi**.
  - Live Wi-Fi Scanner: Scans channels, removes hidden SSIDs, deduplicates, and sorts by signal strength.
  - Quick Connect Form: Autocomplete from scanned networks, password visibility toggle (`👁️`), and connection safety alerts.

### 3. Dynamic Hardware Telemetry (Raspberry Pi 5 Target & Remote Agent)
- **Dynamic RAM Detection:** Reads real-time kernel memory directly from `/proc/meminfo` or `psutil`, dynamically adapting to 2 GB, 4 GB, 8 GB, or 16 GB Pi 5 boards without hardcoded limits:
  $$\text{format: } \texttt{\{used\_gb:.1f\} GB / \{total\_gb:.1f\} GB (\{percent\}\%)}$$
- **Dynamic Storage Detection:** Automatically inspects the root mount point (`/`) via `shutil.disk_usage('/')` or `statvfs`, dynamically handling 32 GB, 64 GB, 128 GB, 256 GB MicroSD cards, NVMe SSDs, or USB drives:
  $$\text{format: } \texttt{\{used\_storage\_gb:.1f\} / \{total\_storage\_gb:.1f\} GB (\{percent\}\%)}$$
- **SoC Temperature (°F):** Real-time thermal reading converted to Fahrenheit:
  $$T_{\text{°F}} = \left(T_{\text{°C}} \times \frac{9}{5}\right) + 32$$
  Categorized with dynamic visual alerts:
  - 🟢 **Cool:** $< 122^\circ\text{F}$
  - 🟡 **Warm:** $122^\circ\text{F}\text{--}158^\circ\text{F}$
  - 🔴 **Hot:** $> 158^\circ\text{F}$
- **Remote vs. Local Guard (`RemoteTelemetryAgent`):**
  - When running on a remote dev machine or secondary server, queries the remote Pi agent via REST API (`/api/system/metrics`), SSH, or serial links.
  - Prevents the host machine's hardware (e.g. AMD64, 32 GB RAM, 1 TB disk) from leaking into the dashboard telemetry.

### 4. Guided Network Builder (Wizard)
- 7-step guided configuration wizard for deploying new DMR or P25 networks from scratch:
  1. **Protocol Selection:** Choose between DMR Duplex or P25 Phase 1 Conventional / Repeater.
  2. **System Identifiers:** Configure DMR Color Code (0–15), Network ID, or P25 NAC (`$293`), System ID (`$001`), and WACN (`$BB800`).
  3. **IDEN Bandplan Calculator:** Built-in APCO P25 bandplan generator (`iden_table.dat`) computing Base Frequency, TX Offset, Channel Spacing (12.5 kHz), and Channel Numbers for 700MHz, 800MHz, and UHF bands.
  4. **Talkgroup Rules:** Provision group voice talkgroups, timeslots (TS1 / TS2), and network routing rules (`talkgroup_rules.yml`).
  5. **Radio ID Access Control (ACL):** Maintain authorized subscriber lists (`rid_acl.dat`) with permit/deny flags.
  6. **Visual Diff Inspection:** Review exact line-by-line configuration diffs before committing any changes to disk.
  7. **Atomic Apply & Verify:** Applies changes safely with automated rollback if the service fails to verify.

### 5. Radio Profile Management & Mutual Hardware Exclusion
- **Shared Hardware Resource Management:** A single MMDVM Duplex modem (dual ADF7021 transceivers) cannot decode DMR and P25 simultaneously due to shared STM32 DSP filters and baseband registers.
- **1-Click Profile Switching:** Seamlessly switch between saved profiles (e.g., *Default DMR Duplex* and *Default P25 Phase 1*).
- **Zero-Corruption Switching Pipeline:**
  1. Validates destination profile configuration.
  2. Creates a timestamped snapshot of the active configuration.
  3. Safely shuts down `dvmhost.service` to release `/dev/ttyAMA0`.
  4. Atomically replaces `config.yml`, `iden_table.dat`, `talkgroup_rules.yml`, and `rid_acl.dat`.
  5. Starts `dvmhost.service`.
  6. Polls service health; automatically restores previous files and restarts if the new profile crashes.

### 6. Protocol-Specific Management Screens
- **DMR Management:** Configure Color Codes, callhang timers, timeslot mapping, talkgroups, peer connections, and issue over-the-air commands (Radio Check, Radio Inhibit, Radio Uninhibit, Alert Page).
- **P25 Phase 1 Management:** Configure NAC, WACN, System ID, Site ID, low-speed data, talkgroup rules, IDEN bandplan tables, and over-the-air subscriber commands.
- **Modem & ADF7021 Transceiver Tuning:** Adjust UART baud rates (115200 bps), serial ports (`/dev/ttyAMA0`), TX/RX inversion, frequency correction offsets (`rxFreqCorrection`, `txFreqCorrection`), and filter bandwidths.

### 7. Transport-Independent Meshtastic Integration
- **Transport Agnostic:** Supports USB Serial (`/dev/ttyUSB0`), TCP / Network (`port 4403`), BLE, and simulated mesh modes.
- **Live Mesh Messenger:** Interactive chat panel with real-time RX/TX activity, channel selection (Ch 0–7), direct messaging, and broadcast messaging.
- **Node Directory & Telemetry:** Discovered nodes table displaying Node ID, Name, Model, Battery Level (%), Battery Voltage, SNR (dB), and GPS coordinates.
- **Bidirectional Radio $\leftrightarrow$ Meshtastic Text Gateway:**
  - **Radio ➔ Meshtastic:** Over-the-air text messages (DMR TMS or P25 PDU) are translated and transmitted onto the designated Meshtastic channel.
  - **Meshtastic ➔ Radio:** Messages typed on Meshtastic handsets (prefixed with `#dmr` or `#p25`) are broadcast over-the-air to the target Talkgroup.
- **Guarded Remote Management:** Authorized operators can issue whitelisted commands over the mesh (`!status`, `!modem`, `!profiles`, `!restart`, `!profile <name>`) with anti-replay protection and 2-step confirmation.

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

DVM Nexus includes fallback mock drivers for hardware telemetry, Wi-Fi networking, and service controllers, enabling full cross-platform testing on any OS (Linux, macOS, Windows).

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/MrRedNeckKYN/DVM-Nexus.git
cd DVM-Nexus

python3 -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Automated Testing

Run the full pytest suite (49 automated unit and integration tests):

```bash
python -m pytest tests -v
```

Tests cover:
- NetworkManager client scanning, failover hotspot toggling, and connect handling (`test_network_manager.py`).
- Dynamic RAM `/proc/meminfo` detection across 2GB, 4GB, 8GB, and 16GB Pi 5 configs (`test_system_monitor.py`).
- Dynamic storage calculations across 32GB, 64GB, 128GB, and 256GB media (`test_system_monitor.py`).
- Remote telemetry agent and host PC leak guard (`test_system_monitor.py`).
- P25 IDEN frequency bandplan calculation (`test_iden_calculator.py`).
- YAML atomic config read/write and snapshot backups (`test_config_engine.py`).
- Mutual exclusion profile switching pipeline (`test_profile_manager.py`).
- Meshtastic text bridging and remote admin whitelists (`test_meshtastic.py`).
- Dual auto-updater pipeline (`test_updater.py`).

### 3. Launch Development Server

```bash
python run_nexus.py --host 0.0.0.0 --port 8080 --reload
```

Open your browser and navigate to `http://localhost:8080`.

---

## Raspberry Pi 5 Auto-Installer & Wi-Fi Failover Setup

### 1. Install DVM Nexus on Raspberry Pi OS (Bookworm)

On a fresh **Raspberry Pi 5**, run the one-line automated installer:

```bash
curl -sSL https://raw.githubusercontent.com/MrRedNeckKYN/DVM-Nexus/main/install.sh | sudo bash
```

Alternatively, from a cloned repository:

```bash
sudo ./install.sh
```

**The installer automatically:**
1. Configures Raspberry Pi 5 GPIO UART (`/dev/ttyAMA0`) and disables serial consoles.
2. Deploys DVM Nexus to `/opt/dvm-nexus` with an isolated virtual environment.
3. Initializes the SQLite database, default DMR/P25 profiles, and Meshtastic routes.
4. Registers and starts `dvm-nexus.service` on boot.
5. Installs the `/usr/local/bin/dvm-nexus` CLI management utility.

### 2. Configure Field Hotspot & Automatic Failover Watchdog

Run the automated hotspot setup script included in the repository:

```bash
sudo bash scripts/setup_hotspot.sh
```

**This script automatically:**
1. Creates the persistent NetworkManager `Field-Hotspot` profile (`SSID: MMDVM-Field-AP`, `Pass: FieldPassword123`, `IP: 10.42.0.1`).
2. Configures priority `100` on your saved home/station Wi-Fi networks.
3. Installs the watchdog script to `/usr/local/bin/wifi-failover.sh`.
4. Installs and enables `wifi-failover.service`.
5. Deploys the sudoers rule in `/etc/sudoers.d/010_dvm_nexus_nmcli` so the web interface can manage Wi-Fi without full root permissions.

---

## REST API Reference

DVM Nexus provides a comprehensive JSON REST API:

### Network & Wi-Fi Management
- `GET /api/network/status`: Returns current mode (`client` vs. `hotspot`), active SSID, IP address, gateway, signal strength, and MAC.
- `POST /api/network/hotspot/toggle`: Payload `{"enable": true|false}`. Activates or deactivates the `Field-Hotspot` AP and sets the manual override flag.
- `GET /api/network/scan`: Returns a deduplicated, signal-sorted JSON list of available Wi-Fi networks.
- `POST /api/network/connect`: Payload `{"ssid": "...", "password": "..."}`. Connects to Wi-Fi and marks it with priority 100.

### System Telemetry & Monitoring
- `GET /api/system/metrics`: Returns real-time SoC temperature (°F and °C), dynamic RAM (`used / total GB`), dynamic storage (`used / total GB`), CPU load, uptime, and hardware label.
- `GET /api/status`: Returns consolidated status including system metrics, `dvmhost.service` status, and active profile metadata.
- `GET /api/stream/events`: Server-Sent Events (SSE) streaming live health telemetry and service state.

### Radio & DVMHost Control
- `GET /api/dvm/status`: Real-time status from DVMHost's REST API.
- `POST /api/dvm/mode`: Switch active operating mode (`dmr`, `p25`, `idle`, `lockout`).
- `POST /api/dvm/service/restart`: Gracefully restart `dvmhost.service`.

---

## System Architecture

```text
+---------------------------------------------------------------------------------------+
|                                  DVM Nexus Web UI                                     |
|     Dual-Viewport Interface: Desktop Persistent Sidebar + Mobile Off-Canvas Drawer     |
|     (HTMX + Jinja2 Templates + Dark Slate Router CSS + 44px Touch Targets)            |
+-------------------------------------------+-------------------------------------------+
                                            | HTTP / SSE
                                            v
+---------------------------------------------------------------------------------------+
|                                 FastAPI Application                                   |
|   - Dashboard Router (/ )                 - DMR Management Router (/dmr)              |
|   - Wi-Fi & Hotspot Router (/network)     - P25 Management Router (/p25)              |
|   - Network Builder Wizard (/wizard)      - Modem & Transceiver Router (/modem)       |
|   - Profile Switcher Router (/profiles)   - Meshtastic Router (/meshtastic)           |
|   - Config History / Diff (/config)       - Auto-Updater Router (/updates)            |
|   - System Telemetry & REST API (/api)                                                |
+-------------------------------------------+-------------------------------------------+
                                            |
        +-----------------------------------+-----------------------------------+
        v                                                                       v
+-------------------------------+                               +-------------------------------+
|     DVM Nexus Config Engine   |                               |         Nexus Core            |
| - IDEN Calculator             |                               | - Dynamic Telemetry (Pi 5)    |
| - Network Schema Validator    |                               | - NetworkManager Controller   |
| - Atomic Backup Manager       |                               | - Service Controller (systemd)|
| - Line-by-Line Diff Engine    |                               | - DVMHost REST API Client     |
| - Profile Manager             |                               | - SQLite Revision DB          |
+---------------+---------------+                               +---------------+---------------+
                |                                                               |
                v                                                               v
+-------------------------------+       +-------------------------------+       +---------------+
|  Active DVM Files (/opt/dvm)  |       |        DVMHost Service        |       | NetworkManager|
| - config.yml                  | <---> | - REST API (Port 9990)        | <---> | (nmcli)       |
| - iden_table.dat              |       | - Serial UART (/dev/ttyAMA0)  |       | Field-Hotspot |
| - talkgroup_rules.yml         |       +---------------+---------------+       +---------------+
| - rid_acl.dat                 |                       |                               |
+-------------------------------+                       v                               v
                                        +-------------------------------+       +---------------+
                                        |   AURSINC MMDVM Duplex Hat    |       | wifi-failover |
                                        | - STM32_HS_GPIO Modem         |       |    daemon     |
                                        | - Dual ADF7021 Transceivers   |       |   (systemd)   |
                                        | - 14.7456 MHz TCXO            |       +---------------+
                                        +-------------------------------+
```

---

## Service Management & Troubleshooting

### Systemd Commands

```bash
# Check DVM Nexus web daemon status
sudo systemctl status dvm-nexus

# Follow live DVM Nexus logs
sudo journalctl -u dvm-nexus -f

# Check Wi-Fi failover watchdog daemon status
sudo systemctl status wifi-failover

# Follow live Wi-Fi watchdog logs
sudo journalctl -u wifi-failover -f

# Check DVMHost radio daemon status
sudo systemctl status dvmhost

# Follow live DVMHost radio logs
sudo journalctl -u dvmhost -f
```

### Configuration & File Locations

- **Active DVMHost Config:** `/opt/dvm/config.yml`
- **IDEN Bandplan Table:** `/opt/dvm/iden_table.dat`
- **Talkgroup Rules:** `/opt/dvm/talkgroup_rules.yml`
- **Radio ID ACL:** `/opt/dvm/rid_acl.dat`
- **Nexus SQLite Database:** `/opt/dvm-nexus/dvm_nexus/data/nexus.db`
- **Configuration Backups:** `/opt/dvm-nexus/dvm_nexus/data/backups/`
- **Saved Radio Profiles:** `/opt/dvm-nexus/dvm_nexus/data/profiles/`
- **Wi-Fi Watchdog Script:** `/usr/local/bin/wifi-failover.sh`
- **Wi-Fi Watchdog Service:** `/etc/systemd/system/wifi-failover.service`
- **Sudoers nmcli Permissions:** `/etc/sudoers.d/010_dvm_nexus_nmcli`

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
