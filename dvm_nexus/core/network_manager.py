"""
DVM Nexus - Network & Wi-Fi Management Controller
Supports NetworkManager (nmcli) on Raspberry Pi 5 with Automatic Failover Hotspot integration.
Includes web-driven auto-provisioning, persistent configuration, and embedded async watchdog.
"""

import os
import json
import shutil
import logging
import subprocess
import time
import tempfile
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

from dvm_nexus.core.config import DATA_DIR
from dvm_nexus.core.database import log_audit

logger = logging.getLogger(__name__)

HOTSPOT_CON_NAME = "Field-Hotspot"
HOTSPOT_CONFIG_FILE = DATA_DIR / "hotspot_config.json"
MANUAL_OVERRIDE_PATH = Path(tempfile.gettempdir()) / "wifi-manual.override"
OVERRIDE_DURATION_SEC = 300  # 5 minutes grace period after manual Web UI toggle

DEFAULT_HOTSPOT_CONFIG: Dict[str, Any] = {
    "ssid": "MMDVM-Field-AP",
    "password": "FieldPassword123",
    "band": "bg",
    "channel": 6,
    "ip_address": "10.42.0.1/24",
    "auto_failover_enabled": True,
    "failover_threshold_sec": 45,
    "watchdog_interval_sec": 20,
}

# Compatibility aliases
HOTSPOT_SSID = DEFAULT_HOTSPOT_CONFIG["ssid"]
HOTSPOT_IP = "10.42.0.1"


class NetworkController:
    """Manages Wi-Fi client and AP Hotspot configurations via NetworkManager (nmcli)."""

    def __init__(self, interface: str = "wlan0"):
        self.interface = interface
        self._has_nmcli = shutil.which("nmcli") is not None
        self._disconnect_counter_sec = 0

        # In-memory simulated state for development/testing when nmcli is unavailable
        self._sim_mode = "client"
        self._sim_connected_ssid = "Station-Wi-Fi-5G"
        self._sim_ip = "192.168.1.185"
        self._sim_mac = "dc:a6:32:8b:4a:12"
        self._sim_gateway = "192.168.1.1"
        self._sim_signal = 88
        self._sim_provisioned = True
        self._sim_available_networks = [
            {"ssid": "Station-Wi-Fi-5G", "signal": 88, "security": "WPA2-PSK", "bars": "▂▄▆█", "in_use": True},
            {"ssid": "MMDVM-Mesh-Node", "signal": 74, "security": "WPA2-PSK", "bars": "▂▄▆_", "in_use": False},
            {"ssid": "ARES-Field-Net", "signal": 62, "security": "WPA2/WPA3", "bars": "▂▄__", "in_use": False},
            {"ssid": "Emergency-Comm-Guest", "signal": 45, "security": "Open", "bars": "▂___", "in_use": False},
        ]

    def _run_cmd(self, cmd: List[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
        """Run nmcli command with optional sudo if needed."""
        if os.name != "nt" and os.geteuid() != 0 and shutil.which("sudo"):
            cmd = ["sudo", "-n"] + cmd
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # ==========================================================================
    # Hotspot Configuration Management (Web-Driven)
    # ==========================================================================

    def get_hotspot_config(self) -> Dict[str, Any]:
        """Load persistent hotspot configuration or defaults."""
        cfg = dict(DEFAULT_HOTSPOT_CONFIG)
        if HOTSPOT_CONFIG_FILE.exists():
            try:
                saved = json.loads(HOTSPOT_CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    cfg.update(saved)
            except Exception as e:
                logger.debug(f"Error loading hotspot config: {e}")
        return cfg

    def save_hotspot_config(self, new_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Save persistent hotspot settings."""
        cfg = self.get_hotspot_config()
        if "ssid" in new_cfg and str(new_cfg["ssid"]).strip():
            cfg["ssid"] = str(new_cfg["ssid"]).strip()
        if "password" in new_cfg and str(new_cfg["password"]).strip():
            cfg["password"] = str(new_cfg["password"]).strip()
        if "band" in new_cfg and new_cfg["band"] in ("bg", "a"):
            cfg["band"] = new_cfg["band"]
        if "channel" in new_cfg:
            try:
                cfg["channel"] = int(new_cfg["channel"])
            except (ValueError, TypeError):
                pass
        if "auto_failover_enabled" in new_cfg:
            cfg["auto_failover_enabled"] = bool(new_cfg["auto_failover_enabled"])
        if "failover_threshold_sec" in new_cfg:
            try:
                cfg["failover_threshold_sec"] = max(15, int(new_cfg["failover_threshold_sec"]))
            except (ValueError, TypeError):
                pass

        try:
            HOTSPOT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            HOTSPOT_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to write hotspot config file: {e}")

        return cfg

    # ==========================================================================
    # Provisioning & Setup (Self-Healing, No Terminal Needed)
    # ==========================================================================

    def is_hotspot_provisioned(self) -> bool:
        """Check if Field-Hotspot profile exists in NetworkManager."""
        if not self._has_nmcli or os.name == "nt":
            return self._sim_provisioned

        try:
            res = self._run_cmd(["nmcli", "-t", "-f", "NAME", "connection", "show"])
            if res.returncode == 0:
                conns = [line.strip() for line in res.stdout.splitlines()]
                return HOTSPOT_CON_NAME in conns
        except Exception:
            pass
        return False

    def provision_hotspot(self, custom_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Create or update the Field-Hotspot connection profile in NetworkManager.
        Can be called directly from the Web UI without terminal/SSH commands.
        """
        if custom_cfg:
            cfg = self.save_hotspot_config(custom_cfg)
        else:
            cfg = self.get_hotspot_config()

        ssid = cfg["ssid"]
        password = cfg["password"]
        band = cfg.get("band", "bg")
        channel = str(cfg.get("channel", 6))
        ip_addr = cfg.get("ip_address", "10.42.0.1/24")

        if not self._has_nmcli or os.name == "nt":
            self._sim_provisioned = True
            log_audit("NETWORK", "HOTSPOT_PROVISION", f"Hotspot '{ssid}' provisioned (simulation)")
            return {
                "success": True,
                "provisioned": True,
                "ssid": ssid,
                "ip": "10.42.0.1",
                "message": f"Field Hotspot '{ssid}' successfully provisioned and ready."
            }

        try:
            already_exists = self.is_hotspot_provisioned()
            if already_exists:
                # Modify existing connection
                cmd = [
                    "nmcli", "connection", "modify", HOTSPOT_CON_NAME,
                    "802-11-wireless.ssid", ssid,
                    "802-11-wireless.mode", "ap",
                    "802-11-wireless.band", band,
                    "802-11-wireless.channel", channel,
                    "802-11-wireless-security.key-mgmt", "wpa-psk",
                    "802-11-wireless-security.psk", password,
                    "ipv4.method", "shared",
                    "ipv4.addresses", ip_addr,
                    "ipv6.method", "ignore",
                    "connection.autoconnect", "no",
                    "connection.autoconnect-priority", "1"
                ]
            else:
                # Add new connection
                cmd = [
                    "nmcli", "connection", "add",
                    "type", "wifi",
                    "ifname", self.interface,
                    "con-name", HOTSPOT_CON_NAME,
                    "autoconnect", "no",
                    "ssid", ssid,
                    "mode", "ap",
                    "802-11-wireless.band", band,
                    "802-11-wireless.channel", channel,
                    "802-11-wireless-security.key-mgmt", "wpa-psk",
                    "802-11-wireless-security.psk", password,
                    "ipv4.method", "shared",
                    "ipv4.addresses", ip_addr,
                    "ipv6.method", "ignore",
                    "connection.autoconnect-priority", "1"
                ]

            res = self._run_cmd(cmd, timeout=20.0)
            if res.returncode == 0:
                # Also synchronize client connection priorities
                self.sync_client_priorities()
                log_audit("NETWORK", "HOTSPOT_PROVISION", f"Field Hotspot '{ssid}' provisioned successfully via Web UI")
                return {
                    "success": True,
                    "provisioned": True,
                    "ssid": ssid,
                    "ip": "10.42.0.1",
                    "message": f"Field Hotspot '{ssid}' provisioned successfully in NetworkManager."
                }
            else:
                err = res.stderr.strip() or res.stdout.strip()
                return {"success": False, "error": f"nmcli error: {err}", "status_code": 500}

        except Exception as e:
            logger.error(f"Failed to provision hotspot: {e}")
            return {"success": False, "error": str(e), "status_code": 500}

    def ensure_hotspot_provisioned(self) -> None:
        """Ensure the Field-Hotspot connection profile is provisioned on server startup."""
        if not self.is_hotspot_provisioned():
            logger.info("Field-Hotspot profile not found. Auto-provisioning via NetworkController...")
            self.provision_hotspot()

    def get_known_wifi_connections(self) -> List[Dict[str, str]]:
        """
        Query NetworkManager for all saved client Wi-Fi connection profiles.
        Returns list of dicts: [{'name': '...', 'ssid': '...', 'uuid': '...'}, ...]
        excluding the failover hotspot profile.
        """
        if not self._has_nmcli or os.name == "nt":
            return [
                {"name": "Station-Wi-Fi-5G", "ssid": "Station-Wi-Fi-5G", "uuid": "sim-uuid-1"},
                {"name": "Home-Network", "ssid": "Home-Network", "uuid": "sim-uuid-2"},
            ]

        results = []
        try:
            res = self._run_cmd(["nmcli", "-t", "-f", "UUID,NAME,TYPE,802-11-wireless.ssid", "connection", "show"])
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.replace(r"\:", "__COLON__").split(":")
                    if len(parts) >= 3:
                        uuid_val = parts[0].replace("__COLON__", ":").strip()
                        name_val = parts[1].replace("__COLON__", ":").strip()
                        type_val = parts[2].replace("__COLON__", ":").strip()
                        ssid_val = parts[3].replace("__COLON__", ":").strip() if len(parts) >= 4 else ""

                        if type_val in ("802-11-wireless", "wifi") and name_val != HOTSPOT_CON_NAME:
                            results.append({
                                "uuid": uuid_val,
                                "name": name_val,
                                "ssid": ssid_val or name_val,
                            })
        except Exception as e:
            logger.error(f"Error fetching known Wi-Fi connections: {e}")

        return results

    def sync_client_priorities(self) -> Dict[str, Any]:
        """Elevate autoconnect-priority to 100 on all saved client Wi-Fi connections."""
        if not self._has_nmcli or os.name == "nt":
            return {"success": True, "count": 2, "message": "Client connection priorities synchronized (simulation)"}

        try:
            conns = self.get_known_wifi_connections()
            count = 0
            for c in conns:
                con_name = c["name"]
                self._run_cmd([
                    "nmcli", "connection", "modify", con_name,
                    "connection.autoconnect", "yes",
                    "connection.autoconnect-priority", "100"
                ])
                count += 1
            return {"success": True, "count": count, "message": f"Prioritized {count} client Wi-Fi connections"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ==========================================================================
    # Manual Override Flag Management
    # ==========================================================================

    def set_manual_override(self) -> None:
        """Mark a manual override flag so the background watchdog doesn't overwrite user action."""
        try:
            MANUAL_OVERRIDE_PATH.write_text(str(time.time() + OVERRIDE_DURATION_SEC), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not write manual override flag: {e}")

    def clear_manual_override(self) -> None:
        """Clear the manual override flag."""
        try:
            if MANUAL_OVERRIDE_PATH.exists():
                MANUAL_OVERRIDE_PATH.unlink()
        except Exception as e:
            logger.debug(f"Could not clear manual override flag: {e}")

    def is_manual_override_active(self) -> bool:
        """Check if manual override flag is active within grace period."""
        try:
            if MANUAL_OVERRIDE_PATH.exists():
                exp = float(MANUAL_OVERRIDE_PATH.read_text(encoding="utf-8").strip())
                if time.time() < exp:
                    return True
                MANUAL_OVERRIDE_PATH.unlink()
        except Exception:
            pass
        return False

    # ==========================================================================
    # Status & Telemetry
    # ==========================================================================

    def get_status(self) -> Dict[str, Any]:
        """
        Query current Wi-Fi status and configuration.
        """
        cfg = self.get_hotspot_config()
        current_hotspot_ssid = cfg["ssid"]

        if not self._has_nmcli or os.name == "nt":
            return {
                "mode": self._sim_mode,
                "active_ssid": current_hotspot_ssid if self._sim_mode == "hotspot" else self._sim_connected_ssid,
                "ip_address": HOTSPOT_IP if self._sim_mode == "hotspot" else self._sim_ip,
                "gateway": "10.42.0.1" if self._sim_mode == "hotspot" else self._sim_gateway,
                "signal_quality": 100 if self._sim_mode == "hotspot" else self._sim_signal,
                "mac_address": self._sim_mac,
                "interface": self.interface,
                "is_connected": bool(self._sim_mode in ("client", "hotspot") and (self._sim_connected_ssid or self._sim_mode == "hotspot")),
                "manual_override": self.is_manual_override_active(),
                "hotspot_provisioned": self._sim_provisioned,
                "auto_failover_enabled": cfg["auto_failover_enabled"],
                "hotspot_config": cfg,
            }

        mode = "disconnected"
        active_ssid = ""
        ip_address = ""
        gateway = ""
        signal = 0
        mac = ""

        try:
            # 1. Check active connections
            res = self._run_cmd(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"])
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split(":")
                    if len(parts) >= 3 and parts[2] == self.interface:
                        con_name = parts[0]
                        if con_name == HOTSPOT_CON_NAME or "hotspot" in con_name.lower():
                            mode = "hotspot"
                            active_ssid = current_hotspot_ssid
                        else:
                            mode = "client"
                            active_ssid = con_name
                        break

            # 2. Get IP and Gateway for wlan0
            res = self._run_cmd(["nmcli", "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,GENERAL.HWADDR", "dev", "show", self.interface])
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if line.startswith("IP4.ADDRESS[1]:"):
                        ip_address = line.split(":", 1)[1].split("/")[0].strip()
                    elif line.startswith("IP4.GATEWAY:"):
                        gateway = line.split(":", 1)[1].strip()
                    elif line.startswith("GENERAL.HWADDR:"):
                        mac = line.split(":", 1)[1].strip()

            # 3. If in client mode, get signal strength of current AP
            if mode == "client":
                res = self._run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi"])
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        if line.startswith("*:"):
                            parts = line.split(":")
                            if len(parts) >= 3:
                                if not active_ssid:
                                    active_ssid = parts[1]
                                try:
                                    signal = int(parts[2])
                                except ValueError:
                                    signal = 0
                            break

            if mode == "hotspot" and not ip_address:
                ip_address = HOTSPOT_IP

        except Exception as e:
            logger.error(f"Error querying network status via nmcli: {e}")

        return {
            "mode": mode,
            "active_ssid": active_ssid,
            "ip_address": ip_address,
            "gateway": gateway,
            "signal_quality": signal if mode == "client" else (100 if mode == "hotspot" else 0),
            "mac_address": mac,
            "interface": self.interface,
            "is_connected": mode in ("client", "hotspot"),
            "manual_override": self.is_manual_override_active(),
            "hotspot_provisioned": self.is_hotspot_provisioned(),
            "auto_failover_enabled": cfg["auto_failover_enabled"],
            "hotspot_config": cfg,
        }

    # ==========================================================================
    # Hotspot Toggle & Client Reconnection
    # ==========================================================================

    def toggle_hotspot(self, enable: bool) -> Dict[str, Any]:
        """
        Activate or deactivate the failover Field Hotspot.
        """
        self.set_manual_override()
        cfg = self.get_hotspot_config()
        current_hotspot_ssid = cfg["ssid"]

        if not self._has_nmcli or os.name == "nt":
            if enable:
                self._sim_mode = "hotspot"
                return {"success": True, "mode": "hotspot", "active_ssid": current_hotspot_ssid, "ip": HOTSPOT_IP, "message": f"Hotspot '{current_hotspot_ssid}' activated"}
            else:
                self._sim_mode = "client"
                self._sim_connected_ssid = "Station-Wi-Fi-5G"
                return {"success": True, "mode": "client", "active_ssid": self._sim_connected_ssid, "ip": self._sim_ip, "message": "Returned to client Wi-Fi"}

        # Ensure provisioned before toggling up
        if enable and not self.is_hotspot_provisioned():
            self.provision_hotspot()

        try:
            if enable:
                res = self._run_cmd(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=20.0)
                if res.returncode == 0:
                    return {
                        "success": True,
                        "mode": "hotspot",
                        "active_ssid": current_hotspot_ssid,
                        "ip": HOTSPOT_IP,
                        "message": f"Field Hotspot '{current_hotspot_ssid}' activated successfully."
                    }
                else:
                    return {
                        "success": False,
                        "error": res.stderr.strip() or res.stdout.strip() or "Failed to start Field-Hotspot",
                        "status_code": 500
                    }
            else:
                self._run_cmd(["nmcli", "connection", "down", HOTSPOT_CON_NAME], timeout=10.0)
                time.sleep(1.0)
                # Try to bring up any known saved client Wi-Fi connection
                known = self.get_known_wifi_connections()
                connected = False
                for conn in known:
                    res = self._run_cmd(["nmcli", "connection", "up", conn["name"]], timeout=15.0)
                    if res.returncode == 0:
                        connected = True
                        break
                if not connected:
                    self._run_cmd(["nmcli", "device", "connect", self.interface], timeout=15.0)
                time.sleep(1.5)
                status = self.get_status()
                return {
                    "success": True,
                    "mode": status["mode"],
                    "active_ssid": status["active_ssid"],
                    "ip": status["ip_address"],
                    "message": f"Deactivated hotspot. Status is now {status['mode']} ({status.get('active_ssid') or 'scanning'})."
                }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Operation timed out", "status_code": 504}
        except Exception as e:
            logger.error(f"Error toggling hotspot: {e}")
            return {"success": False, "error": str(e), "status_code": 500}

    # ==========================================================================
    # Wi-Fi Scanning & Connection
    # ==========================================================================

    def scan_networks(self) -> List[Dict[str, Any]]:
        """
        Trigger a Wi-Fi scan and return sanitized, deduplicated, sorted network list.
        """
        if not self._has_nmcli or os.name == "nt":
            result = []
            for net in self._sim_available_networks:
                item = dict(net)
                item["in_use"] = (self._sim_mode == "client" and item["ssid"] == self._sim_connected_ssid)
                result.append(item)
            return sorted(result, key=lambda x: x["signal"], reverse=True)

        networks_dict: Dict[str, Dict[str, Any]] = {}
        try:
            res = self._run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,BARS", "dev", "wifi", "list", "--rescan", "yes"], timeout=20.0)
            if res.returncode != 0:
                res = self._run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,BARS", "dev", "wifi", "list"], timeout=10.0)

            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.replace(r"\:", "__COLON__").split(":")
                    if len(parts) >= 5:
                        in_use = (parts[0].strip() == "*")
                        ssid = parts[1].replace("__COLON__", ":").strip()
                        raw_signal = parts[2].strip()
                        security = parts[3].strip() or "Open"
                        bars = parts[4].strip()

                        if not ssid or ssid == "--":
                            continue

                        try:
                            signal_val = int(raw_signal)
                        except ValueError:
                            signal_val = 0

                        if ssid not in networks_dict or signal_val > networks_dict[ssid]["signal"]:
                            networks_dict[ssid] = {
                                "ssid": ssid,
                                "signal": signal_val,
                                "security": security,
                                "bars": bars,
                                "in_use": in_use
                            }
        except Exception as e:
            logger.error(f"Error scanning Wi-Fi networks: {e}")

        return sorted(networks_dict.values(), key=lambda x: x["signal"], reverse=True)

    def connect_network(self, ssid: str, password: Optional[str] = None, timeout: float = 25.0) -> Dict[str, Any]:
        """
        Connect to a Wi-Fi network and set autoconnect-priority to 100.
        If in Field-Hotspot mode, safely tears down the hotspot before connecting,
        and automatically restores Field-Hotspot if connection fails.
        """
        if not ssid or not ssid.strip():
            return {"success": False, "error": "SSID cannot be empty", "status_code": 400}

        ssid = ssid.strip()
        pwd = (password or "").strip()
        self.set_manual_override()

        if not self._has_nmcli or os.name == "nt":
            if ssid.lower() == "failnetwork":
                return {
                    "success": False,
                    "error": f"Failed to connect to '{ssid}': Invalid Wi-Fi password. Field-Hotspot was restored.",
                    "status_code": 401
                }
            self._sim_mode = "client"
            self._sim_connected_ssid = ssid
            self._sim_ip = "192.168.1.185"
            return {
                "success": True,
                "ssid": ssid,
                "ip": self._sim_ip,
                "message": f"Successfully connected to Wi-Fi network '{ssid}'."
            }

        try:
            status_before = self.get_status()
            was_hotspot = (status_before.get("mode") == "hotspot")

            # Check if a connection profile already exists for this SSID
            known_conns = self.get_known_wifi_connections()
            existing_conn = None
            for conn in known_conns:
                if conn["ssid"].strip() == ssid or conn["name"].strip() == ssid:
                    existing_conn = conn
                    break

            profile_name = existing_conn["name"] if existing_conn else ssid

            # Update or create the connection profile with credentials and top autoconnect priority
            if existing_conn:
                mod_cmd = [
                    "nmcli", "connection", "modify", profile_name,
                    "802-11-wireless.ssid", ssid,
                    "connection.autoconnect", "yes",
                    "connection.autoconnect-priority", "100"
                ]
                if pwd:
                    mod_cmd.extend([
                        "802-11-wireless-security.key-mgmt", "wpa-psk",
                        "802-11-wireless-security.psk", pwd
                    ])
                self._run_cmd(mod_cmd)
            else:
                add_cmd = [
                    "nmcli", "connection", "add",
                    "type", "wifi",
                    "ifname", self.interface,
                    "con-name", ssid,
                    "ssid", ssid,
                    "connection.autoconnect", "yes",
                    "connection.autoconnect-priority", "100"
                ]
                if pwd:
                    add_cmd.extend([
                        "wifi-sec.key-mgmt", "wpa-psk",
                        "wifi-sec.psk", pwd
                    ])
                add_res = self._run_cmd(add_cmd)
                if add_res.returncode != 0:
                    logger.warning(f"nmcli connection add warning: {add_res.stderr.strip()}")

            # If Field-Hotspot is active on wlan0, tear it down so the radio switches to station mode
            if was_hotspot:
                logger.info(f"Releasing wlan0 from AP mode (Field-Hotspot) before connecting to '{ssid}'...")
                self._run_cmd(["nmcli", "connection", "down", HOTSPOT_CON_NAME], timeout=10.0)
                time.sleep(1.5)

            # Bring up the target client connection
            logger.info(f"Activating client connection '{profile_name}' for SSID '{ssid}'...")
            res = self._run_cmd(["nmcli", "connection", "up", profile_name], timeout=timeout)

            # Fallback attempt via direct dev wifi connect if profile activation failed
            if res.returncode != 0:
                logger.warning(f"Direct connection up failed ({res.stderr.strip()}). Retrying via dev wifi connect...")
                direct_cmd = ["nmcli", "dev", "wifi", "connect", ssid]
                if pwd:
                    direct_cmd.extend(["password", pwd])
                res = self._run_cmd(direct_cmd, timeout=timeout)

            # Check outcome
            if res.returncode == 0:
                self._run_cmd([
                    "nmcli", "connection", "modify", profile_name,
                    "connection.autoconnect", "yes",
                    "connection.autoconnect-priority", "100"
                ])
                time.sleep(2.0)
                status = self.get_status()
                assigned_ip = status.get("ip_address", "")
                if not assigned_ip:
                    time.sleep(2.0)
                    status = self.get_status()
                    assigned_ip = status.get("ip_address", "")

                return {
                    "success": True,
                    "ssid": ssid,
                    "ip": assigned_ip or "DHCP pending",
                    "message": f"Connected to '{ssid}' successfully. IP assigned: {assigned_ip or 'DHCP pending'}"
                }

            # If connection failed: SAFETY ROLLBACK TO FIELD-HOTSPOT
            err = res.stderr.strip() or res.stdout.strip()
            logger.error(f"Failed to connect to '{ssid}': {err}")

            if was_hotspot:
                logger.warning(f"Connection to '{ssid}' failed. Restoring Field-Hotspot so user is not stranded...")
                self._run_cmd(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=25.0)

            if "Secrets were required" in err or "password" in err.lower():
                return {
                    "success": False,
                    "error": f"Failed to connect to '{ssid}': Incorrect Wi-Fi password. Field-Hotspot was restored.",
                    "status_code": 401
                }
            elif "No network with SSID" in err:
                return {
                    "success": False,
                    "error": f"Network '{ssid}' was not found in range. Field-Hotspot was restored.",
                    "status_code": 404
                }
            return {
                "success": False,
                "error": f"Failed to connect to '{ssid}' ({err}). Field-Hotspot was restored.",
                "status_code": 500
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Connection to '{ssid}' timed out.")
            if was_hotspot:
                self._run_cmd(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=25.0)
            return {
                "success": False,
                "error": f"Connection to '{ssid}' timed out after {int(timeout)}s. Field-Hotspot was restored.",
                "status_code": 504
            }
        except Exception as e:
            logger.error(f"Error connecting to Wi-Fi '{ssid}': {e}")
            if was_hotspot:
                self._run_cmd(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=25.0)
            return {"success": False, "error": str(e), "status_code": 500}

    # ==========================================================================
    # Embedded Async Failover Watchdog
    # ==========================================================================

    def watchdog_step(self) -> Dict[str, Any]:
        """
        Execute one failover check cycle.
        Returns diagnostic outcome of the cycle.
        """
        cfg = self.get_hotspot_config()
        if not cfg.get("auto_failover_enabled", True):
            self._disconnect_counter_sec = 0
            return {"action": "disabled", "message": "Auto failover watchdog is disabled in settings"}

        if self.is_manual_override_active():
            self._disconnect_counter_sec = 0
            return {"action": "override", "message": "Manual override is active, watchdog paused"}

        status = self.get_status()
        current_mode = status["mode"]
        threshold = cfg.get("failover_threshold_sec", 45)
        interval = cfg.get("watchdog_interval_sec", 20)

        # Case 1: In Hotspot mode -> scan for known client SSIDs to reconnect
        if current_mode == "hotspot":
            self._disconnect_counter_sec = 0
            scanned = self.scan_networks()
            scanned_ssids = {n["ssid"] for n in scanned if n.get("ssid")}

            # Get known client SSIDs and connection profiles from NetworkManager
            known_conns = self.get_known_wifi_connections()
            target_profile = None
            target_ssid = None

            for conn in known_conns:
                conn_ssid = conn.get("ssid") or conn.get("name")
                conn_name = conn.get("name")
                if conn_ssid in scanned_ssids or conn_name in scanned_ssids:
                    target_profile = conn_name
                    target_ssid = conn_ssid
                    break

            if target_profile:
                logger.info(f"[Watchdog] Known network '{target_ssid}' ({target_profile}) visible in scan! Reconnecting...")
                if not self._has_nmcli or os.name == "nt":
                    self._sim_mode = "client"
                    self._sim_connected_ssid = target_ssid
                    log_audit("WATCHDOG", "AUTO_RECONNECT", f"Reconnected to client network '{target_ssid}' (simulation)")
                    return {"action": "reconnect", "target": target_ssid, "success": True}

                self._run_cmd(["nmcli", "connection", "down", HOTSPOT_CON_NAME], timeout=10.0)
                time.sleep(1.5)
                res = self._run_cmd(["nmcli", "connection", "up", target_profile], timeout=20.0)
                if res.returncode == 0:
                    log_audit("WATCHDOG", "AUTO_RECONNECT", f"Reconnected to client network '{target_ssid}'")
                    return {"action": "reconnect", "target": target_ssid, "success": True}
                else:
                    logger.warning(f"[Watchdog] Reconnect to '{target_ssid}' failed. Restoring Field-Hotspot...")
                    self._run_cmd(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=25.0)
                    return {"action": "reconnect_failed", "target": target_ssid, "success": False}

            return {"action": "hotspot_active", "message": "Hotspot active, waiting for known Wi-Fi in scan"}

        # Case 2: In Client mode and connected
        elif current_mode == "client" and status["is_connected"] and status.get("ip_address"):
            self._disconnect_counter_sec = 0
            return {"action": "client_healthy", "ip": status["ip_address"]}

        # Case 3: Disconnected from client Wi-Fi
        else:
            self._disconnect_counter_sec += interval
            logger.info(f"[Watchdog] Wi-Fi disconnected ({self._disconnect_counter_sec}/{threshold}s)")

            if self._disconnect_counter_sec >= threshold:
                logger.warning(f"[Watchdog] Disconnected for {self._disconnect_counter_sec}s >= threshold {threshold}s. Bringing UP Field-Hotspot...")
                res = self.toggle_hotspot(True)
                self._disconnect_counter_sec = 0
                log_audit("WATCHDOG", "AUTO_FAILOVER", f"Triggered automatic failover to Field Hotspot: {res.get('message')}")
                return {"action": "failover_triggered", "result": res}

            return {"action": "counting", "disconnected_sec": self._disconnect_counter_sec, "threshold": threshold}

            return {"action": "counting", "disconnected_sec": self._disconnect_counter_sec, "threshold": threshold}

    async def run_watchdog_loop(self) -> None:
        """Asynchronous background loop running inside DVM Nexus."""
        logger.info("[Watchdog] Embedded Wi-Fi Failover Watchdog started.")
        while True:
            try:
                cfg = self.get_hotspot_config()
                interval = cfg.get("watchdog_interval_sec", 20)
                await asyncio.to_thread(self.watchdog_step)
            except asyncio.CancelledError:
                logger.info("[Watchdog] Watchdog loop cancelled.")
                break
            except Exception as e:
                logger.debug(f"[Watchdog] Error in watchdog loop step: {e}")
                interval = 20
            await asyncio.sleep(interval)


network_controller = NetworkController()
