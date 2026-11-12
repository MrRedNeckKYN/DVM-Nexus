# DVM Nexus

[![Version](https://img.shields.io/badge/version-1.0.51-cyan.svg)](https://github.com/MrRedNeckKYN/DVM-Nexus/releases)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com)
[![Hardware](https://img.shields.io/badge/hardware-Raspberry%20Pi%205-red.svg)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Modem](https://img.shields.io/badge/modem-MMDVM%20Duplex%20Hat-blueviolet.svg)](#hardware-setup)
[![Tests](https://img.shields.io/badge/tests-91%20passed-brightgreen.svg)](https://github.com/MrRedNeckKYN/DVM-Nexus)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**DVM Nexus** is a next-generation web management operating system and configuration manager for [DVMHost](https://github.com/DVMProject/dvmhost) and [DVMProject](https://github.com/DVMProject) repeaters/hotspots.

Instead of SSHing into your Raspberry Pi to edit YAML configs, manually calculating P25 frequency tables, or getting locked out when your Wi-Fi changes in the field, DVM Nexus gives you a router-grade web interface to control your repeater, switch modes, monitor hardware, manage connections, and bridge radio voice traffic to Meshtastic LoRa from any phone, tablet, or PC.

---

## 🌟 Core Features & Architecture

### 🎙️ Bidirectional Voice Bridge (Voice-to-Text STT & Text-to-Speech TTS)
- **Local Offline Speech-to-Text (STT):** Captures incoming DMR and P25 voice traffic from `dvmbridge` over UDP PCM (8kHz 16-bit linear PCM), transcribes transmissions using `faster-whisper` (`tiny.en` or `base.en`), and broadcasts the transcript with caller Radio ID and talkgroup directly to Meshtastic channels.
- **Local Offline Text-to-Speech (TTS):** Converts incoming Meshtastic text messages into synthesized voice using `piper-tts`, streaming PCM frames in 20ms chunks to `dvmbridge:32001` to key up the duplex hat on a dedicated voice talkgroup.
- **Acoustic Echo-Lockout Cooldown:** Built-in hardware lockout cooldown (default: 3.0s) prevents the duplex hat's own TTS transmissions from re-triggering the STT receiver in an infinite acoustic loop.
- **Dedicated Voice Talkgroups:** Route voice translation exclusively through dedicated talkgroups (e.g., TG 99 - Mesh Gateway) without disturbing primary repeaters.
- **Live Activity Feed:** Auto-refreshing real-time feed with caller Radio ID, talkgroup, audio duration, and transcript history. Built-in simulation tool to test Whisper and Meshtastic dispatch without a physical radio.

### 📱 Modern Dashboard for Desktop & Mobile
- **Live Hardware Telemetry:** Real-time SoC temperature in °F, CPU load, RAM usage, storage space, and uptime.
- **Process Supervision & Crash Banners:** Start, stop, or restart `dvmhost` with 1 click. If the daemon crashes or fails to start, an alert banner reveals the exact exit code and root cause with 1-click auto-repair.
- **Responsive Mobile First:** Fully optimized for mobile screens with an off-canvas drawer, quick bottom navigation bar, and touch-friendly controls.
- **Live Event Stream:** Real-time terminal log viewer, connection events, and radio traffic without refreshing the page.

### 🩺 System Diagnostics, Health Doctor & 1-Click Auto-Repair
- **Web-Based Terminal Logs:** Live color-coded `journalctl -u dvmhost.service` output directly in your browser.
- **Automated Health Check Doctor:** Detects misconfigurations before they crash your hotspot:
  - Validates `iAgreeNotToBeStupid` and `daemon: false` policies.
  - Verifies Linux hardware UART access (`/dev/ttyAMA0`) and serial-getty conflicts.
  - Checks for the "Dummy Null Modem" simulator trap and enforces real hardware UART.
  - Resolves Voice Channel RPC collisions (loopback port conflicts) and non-authoritative master lockouts.
  - Validates `iden_table.dat` TX frequency offsets (replaces invalid 0.0 MHz entries with +5.0 MHz).
- **1-Click Auto-Repair:** Automatically remedies identified configuration flaws, fixes permissions, restores missing baseline tables, and restarts the daemon cleanly.

### 📻 Native Protocol Management (DMR & P25 Phase 1)
- **Auto-Saving Active Config:** Modifying parameters on `/dmr` or `/p25` automatically updates `/opt/dvm/config.yml` and `/opt/dvm/iden_table.dat` with safety backups.
- **Full Duplex & Simplex Direct Talkaround:** Full support for full-duplex repeaters (+5 MHz offset) and single-frequency direct simplex (e.g. GMRS 462.5625 MHz), automatically managing `simplexSameFrequency: true` and hiding redundant RX boxes.
- **Visual Talkgroup Rules & Presets:** Add, view, and delete talkgroup routing rules (`groupVoice:`) with quick presets (World-Wide, Local, Parrot TG 9990, All Call TG 65535).
- **Over-The-Air (OTA) Radio Commands:** Send live P25 Radio Check, Call Alert / Page, Radio Inhibit (Stun), and Uninhibit directly to target Radio IDs over the air from the browser.
- **Integrated REST & RPC API Controls:** Enable, disable, and configure DVMHost's internal REST/RPC remote control daemon and change passwords directly in the UI.

### 📶 Automatic Failover Hotspot (Field-AP)
- **Never Get Locked Out:** If your home Wi-Fi drops or you take your hotspot into the field, the Raspberry Pi automatically spins up its own Wi-Fi access point (`MMDVM-Field-AP` at `10.42.0.1`).
- **Scan & Connect on Mobile:** Connect to the Field-AP from your smartphone, open the web UI, scan for available local Wi-Fi networks, and connect with a single tap.
- **Auto-Reconnect Home:** When your home Wi-Fi comes back in range, the failover watchdog automatically disables the AP and reconnects to your client network.
- **Zero Shell Scripts:** 100% managed via NetworkManager through the **Wi-Fi & Hotspot** panel.

### 🔄 1-Click Radio Profile Switching
- **Hardware Register Safety:** MMDVM Duplex hats cannot decode DMR and P25 simultaneously due to shared ADF7021 hardware registers. Nexus manages single-protocol mutual exclusion to prevent modem `NAK 32 (HS_NO_DUAL_MODE)` crashes.
- **Safe Mode Switching:** Switch between saved radio profiles with 1 click. Nexus creates a backup snapshot, releases the hardware serial port (`/dev/ttyAMA0`), applies new configurations, and restarts `dvmhost`. If startup fails, it automatically rolls back.

### 💻 Web Interactive Console
- **Embedded Root Terminal:** Direct browser-based shell access for hardware pin debugging, serial inspection, and system commands.
- **Command History & ANSI Support:** Supports command history (Up/Down arrows), ANSI color rendering, output clipboard copying, and screen clearing (Ctrl+L).

### 🚀 1-Click In-Place Updates
- Check for updates and install new releases of DVM Nexus and DVMHost directly from the **System Updates** page.
- Backs up your configuration, pulls upstream changes, applies migrations, and restarts the service automatically.

---

## 🔌 Hardware Setup

DVM Nexus is optimized for the **Raspberry Pi 5** paired with an **AURSINC MMDVM Duplex Hotspot Hat** (or compatible STM32 ADF7021 duplex boards):

- **Modem Firmware:** `STM32_HS_GPIO` (dual ADF7021 transceivers).
- **Oscillator (TCXO):** 14.7456 MHz.
- **Serial Interface:** Raspberry Pi 40-pin GPIO UART (`/dev/ttyAMA0` @ 115200 bps).

### GPIO Pin Map

| Pin | Signal | Pi 5 Pin | Hat Pin | Description |
|:---:|:---:|:---:|:---:|:---|
| **01** | `3.3V` | Pin 1 | 3.3V | Powers STM32 microcontroller logic |
| **02** | `5.0V` | Pin 2 | 5V | Powers ADF7021 RF stages |
| **06** | `GND` | Pin 6 | GND | Common ground reference |
| **08** | `TXD` | GPIO 14 (Pin 8) | RXD | Pi transmits commands/frames to modem |
| **10** | `RXD` | GPIO 15 (Pin 10) | TXD | Modem sends decoded RF frames to Pi |

> [!IMPORTANT]
> **Duplex Hat Antenna Notice:** Duplex MMDVM hats have two separate SMA antenna jacks: one dedicated to **TX** and one dedicated to **RX**. Always install antennas on **both** ports (or place your single antenna on **RX** when running in Simplex mode).

---

## 📦 Installation on Raspberry Pi

On a fresh install of Raspberry Pi OS (Debian 12 Bookworm, 64-bit recommended):

```bash
curl -sSL https://raw.githubusercontent.com/MrRedNeckKYN/DVM-Nexus/main/install.sh | sudo bash
```

Or clone the repository directly:

```bash
git clone https://github.com/MrRedNeckKYN/DVM-Nexus.git /opt/dvm-nexus
cd /opt/dvm-nexus
sudo ./install.sh
```

### What the installer configures:
1. Enables Raspberry Pi GPIO UART (`/dev/ttyAMA0`) and masks `serial-getty@ttyAMA0.service`.
2. Installs DVM Nexus into `/opt/dvm-nexus` with an isolated Python virtual environment.
3. Initializes the SQLite database, default DMR/P25 profiles, and talkgroup tables.
4. Configures the automatic failover access point (`MMDVM-Field-AP`).
5. Registers and starts the `dvm-nexus` systemd service on boot.

Access the web UI from your browser:
```text
http://<raspberry-pi-ip>:8080
```

---

## 🛠️ Local Development & Testing

DVM Nexus includes comprehensive mock drivers for hardware telemetry, Wi-Fi interfaces, Meshtastic nodes, and radio daemons, enabling complete local development and testing on Windows, macOS, or Linux without attached radio hardware.

```bash
# 1. Clone the repository
git clone https://github.com/MrRedNeckKYN/DVM-Nexus.git
cd DVM-Nexus

# 2. Set up virtual environment
python3 -m venv venv
# Windows:
.\venv\Scripts\Activate.ps1
# Linux / macOS:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run test suite (82 passing tests)
python -m pytest

# 5. Start development server
python run_nexus.py --host 0.0.0.0 --port 8080 --reload
```

---

## 📂 Key File Locations

| File / Directory | Description |
|:---|:---|
| `/opt/dvm/config.yml` | Active live DVMHost configuration file |
| `/opt/dvm/iden_table.dat` | Active P25 IDEN bandplan frequency table |
| `/opt/dvm/talkgroup_rules.yml` | Active DMR & P25 talkgroup routing rules |
| `/opt/dvm/rid_acl.dat` | Active Radio ID access control list |
| `/opt/dvm-nexus/dvm_nexus/data/nexus.db` | SQLite database (profiles, audit logs, mesh messages, voice logs) |
| `/opt/dvm-nexus/dvm_nexus/data/backups/` | Automated timestamped configuration snapshots & rollback diffs |
| `/opt/dvm-nexus/dvm_nexus/data/profiles/` | Saved radio mode profiles (`default_dmr`, `default_p25`) |

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
