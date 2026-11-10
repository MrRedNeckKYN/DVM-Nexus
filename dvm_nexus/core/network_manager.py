"""
DVM Nexus - Network & Wi-Fi Management Controller
Supports NetworkManager (nmcli) on Raspberry Pi 5 with Automatic Failover Hotspot integration.
Includes graceful fallback simulation for cross-platform development.
"""

import os
import shutil
import logging
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

HOTSPOT_CON_NAME = "Field-Hotspot"
HOTSPOT_SSID = "MMDVM-Field-AP"
HOTSPOT_IP = "10.42.0.1"
# /tmp/wifi-manual.override on Linux, %TEMP%\wifi-manual.override on Windows
MANUAL_OVERRIDE_PATH = Path(tempfile.gettempdir()) / "wifi-manual.override"
OVERRIDE_DURATION_SEC = 300  # 5 minutes grace period after manual Web UI toggle


class NetworkController:
    """Manages Wi-Fi client and AP Hotspot configurations via NetworkManager (nmcli)."""

    def __init__(self, interface: str = "wlan0"):
        self.interface = interface
        self._has_nmcli = shutil.which("nmcli") is not None
        # In-memory simulated state for development/testing when nmcli is unavailable
        self._sim_mode = "client"
        self._sim_connected_ssid = "Station-Wi-Fi-5G"
        self._sim_ip = "192.168.1.185"
        self._sim_mac = "dc:a6:32:8b:4a:12"
        self._sim_gateway = "192.168.1.1"
        self._sim_signal = 88
        self._sim_available_networks = [
            {"ssid": "Station-Wi-Fi-5G", "signal": 88, "security": "WPA2-PSK", "bars": "▂▄▆█", "in_use": True},
            {"ssid": "MMDVM-Mesh-Node", "signal": 74, "security": "WPA2-PSK", "bars": "▂▄▆_", "in_use": False},
            {"ssid": "ARES-Field-Net", "signal": 62, "security": "WPA2/WPA3", "bars": "▂▄__", "in_use": False},
            {"ssid": "Emergency-Comm-Guest", "signal": 45, "security": "Open", "bars": "▂___", "in_use": False},
        ]

    def _run_cmd(self, cmd: List[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
        """Run nmcli command with optional sudo if needed."""
        # Check if sudo is needed
        if os.name != "nt" and os.geteuid() != 0 and shutil.which("sudo"):
            cmd = ["sudo", "-n"] + cmd
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

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

    def get_status(self) -> Dict[str, Any]:
        """
        Query current Wi-Fi status.
        Returns: {
            "mode": "client" | "hotspot" | "disconnected",
            "active_ssid": str,
            "ip_address": str,
            "gateway": str,
            "signal_quality": int,
            "mac_address": str,
            "interface": str,
            "is_connected": bool,
            "manual_override": bool
        }
        """
        if not self._has_nmcli or os.name == "nt":
            return {
                "mode": self._sim_mode,
                "active_ssid": HOTSPOT_SSID if self._sim_mode == "hotspot" else self._sim_connected_ssid,
                "ip_address": HOTSPOT_IP if self._sim_mode == "hotspot" else self._sim_ip,
                "gateway": "10.42.0.1" if self._sim_mode == "hotspot" else self._sim_gateway,
                "signal_quality": 100 if self._sim_mode == "hotspot" else self._sim_signal,
                "mac_address": self._sim_mac,
                "interface": self.interface,
                "is_connected": bool(self._sim_mode in ("client", "hotspot") and (self._sim_connected_ssid or self._sim_mode == "hotspot")),
                "manual_override": self.is_manual_override_active(),
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
                            active_ssid = HOTSPOT_SSID
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
        }

    def toggle_hotspot(self, enable: bool) -> Dict[str, Any]:
        """
        Activate or deactivate the failover Field Hotspot.
        - enable = True: bring up "Field-Hotspot"
        - enable = False: bring down "Field-Hotspot" and connect to known client Wi-Fi
        """
        self.set_manual_override()

        if not self._has_nmcli or os.name == "nt":
            if enable:
                self._sim_mode = "hotspot"
                return {"success": True, "mode": "hotspot", "active_ssid": HOTSPOT_SSID, "ip": HOTSPOT_IP, "message": f"Hotspot '{HOTSPOT_SSID}' activated"}
            else:
                self._sim_mode = "client"
                self._sim_connected_ssid = "Station-Wi-Fi-5G"
                return {"success": True, "mode": "client", "active_ssid": self._sim_connected_ssid, "ip": self._sim_ip, "message": "Returned to client Wi-Fi"}

        try:
            if enable:
                # Bring up Field-Hotspot
                res = self._run_cmd(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=20.0)
                if res.returncode == 0:
                    return {
                        "success": True,
                        "mode": "hotspot",
                        "active_ssid": HOTSPOT_SSID,
                        "ip": HOTSPOT_IP,
                        "message": f"Field Hotspot '{HOTSPOT_SSID}' activated successfully."
                    }
                else:
                    return {
                        "success": False,
                        "error": res.stderr.strip() or res.stdout.strip() or "Failed to start Field-Hotspot",
                        "status_code": 500
                    }
            else:
                # Bring down Field-Hotspot and let NetworkManager auto-connect or connect wlan0
                self._run_cmd(["nmcli", "connection", "down", HOTSPOT_CON_NAME], timeout=10.0)
                res = self._run_cmd(["nmcli", "device", "connect", self.interface], timeout=20.0)
                status = self.get_status()
                return {
                    "success": True,
                    "mode": status["mode"],
                    "active_ssid": status["active_ssid"],
                    "ip": status["ip_address"],
                    "message": f"Deactivated hotspot. Client mode is now {status['mode']}."
                }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Operation timed out", "status_code": 504}
        except Exception as e:
            logger.error(f"Error toggling hotspot: {e}")
            return {"success": False, "error": str(e), "status_code": 500}

    def scan_networks(self) -> List[Dict[str, Any]]:
        """
        Trigger a Wi-Fi scan and return sanitized, deduplicated, sorted network list.
        Executes: nmcli -t -f SSID,SIGNAL,SECURITY,BARS dev wifi list --rescan yes
        """
        if not self._has_nmcli or os.name == "nt":
            # Return simulated networks with current active marked
            result = []
            for net in self._sim_available_networks:
                item = dict(net)
                item["in_use"] = (self._sim_mode == "client" and item["ssid"] == self._sim_connected_ssid)
                result.append(item)
            return sorted(result, key=lambda x: x["signal"], reverse=True)

        networks_dict: Dict[str, Dict[str, Any]] = {}
        try:
            # Rescan Wi-Fi
            res = self._run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,BARS", "dev", "wifi", "list", "--rescan", "yes"], timeout=20.0)
            if res.returncode != 0:
                # Try without --rescan yes if adapter is busy
                res = self._run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,BARS", "dev", "wifi", "list"], timeout=10.0)

            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # nmcli colon separated format
                    # Handle escaped colons (\:) in SSIDs
                    parts = line.replace(r"\:", "__COLON__").split(":")
                    if len(parts) >= 5:
                        in_use = (parts[0].strip() == "*")
                        ssid = parts[1].replace("__COLON__", ":").strip()
                        raw_signal = parts[2].strip()
                        security = parts[3].strip() or "Open"
                        bars = parts[4].strip()

                        # Filter out empty or hidden SSIDs (nmcli outputs '--')
                        if not ssid or ssid == "--":
                            continue

                        try:
                            signal_val = int(raw_signal)
                        except ValueError:
                            signal_val = 0

                        # Deduplicate: if duplicate SSID found, keep highest signal
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

        # Convert to list and sort by signal descending
        sorted_list = sorted(networks_dict.values(), key=lambda x: x["signal"], reverse=True)
        return sorted_list

    def connect_network(self, ssid: str, password: Optional[str] = None, timeout: float = 25.0) -> Dict[str, Any]:
        """
        Connect to a Wi-Fi network and set autoconnect-priority to 100.
        Executes: nmcli dev wifi connect "<ssid>" password "<password>"
        """
        if not ssid or not ssid.strip():
            return {"success": False, "error": "SSID cannot be empty", "status_code": 400}

        ssid = ssid.strip()
        self.set_manual_override()

        if not self._has_nmcli or os.name == "nt":
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
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
            if password and password.strip():
                cmd.extend(["password", password.strip()])

            res = self._run_cmd(cmd, timeout=timeout)
            if res.returncode == 0:
                # Connection succeeded; set high priority so Pi prefers this network over Hotspot
                self._run_cmd(["nmcli", "connection", "modify", ssid, "connection.autoconnect", "yes", "connection.autoconnect-priority", "100"])

                # Retrieve new IP
                time.sleep(1.5)
                status = self.get_status()
                return {
                    "success": True,
                    "ssid": ssid,
                    "ip": status.get("ip_address", ""),
                    "message": f"Connected to '{ssid}' successfully. IP assigned: {status.get('ip_address', 'DHCP pending')}"
                }
            else:
                err = res.stderr.strip() or res.stdout.strip()
                if "Secrets were required" in err or "password" in err.lower():
                    return {"success": False, "error": "Invalid Wi-Fi password. Please check credentials and retry.", "status_code": 401}
                elif "No network with SSID" in err:
                    return {"success": False, "error": f"Network '{ssid}' was not found in range.", "status_code": 404}
                return {"success": False, "error": err or "Failed to connect to network", "status_code": 500}

        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Connection to '{ssid}' timed out after {int(timeout)} seconds.", "status_code": 504}
        except Exception as e:
            logger.error(f"Error connecting to Wi-Fi '{ssid}': {e}")
            return {"success": False, "error": str(e), "status_code": 500}


network_controller = NetworkController()
