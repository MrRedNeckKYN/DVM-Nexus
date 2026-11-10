"""
DVM Nexus - Systemd Service Controller
Manages dvmhost.service with verification, journal logs, and development fallback.
"""

import subprocess
import shutil
import time
import os
import psutil
from typing import Dict, Any, List, Optional
from dvm_nexus.core.config import settings
from dvm_nexus.core.database import log_audit


class ServiceController:
    """Controls the dvmhost systemd service."""

    def __init__(self, service_name: Optional[str] = None):
        self.service_name = service_name or settings.dvmhost_service_name
        self.has_systemctl = shutil.which("systemctl") is not None and os.name != "nt"
        # Simulated state for development/testing
        self._dev_simulated_running = False
        self._dev_simulated_start_time = None

    def get_status(self) -> Dict[str, Any]:
        """Query service status and PID."""
        if not self.has_systemctl:
            # Check if dvmhost process is running via psutil
            pids = []
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if "dvmhost" in p.info["name"].lower():
                        pids.append(p.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            is_running = len(pids) > 0 or self._dev_simulated_running
            return {
                "service": self.service_name,
                "is_running": is_running,
                "state": "active (running)" if is_running else "inactive (dead)",
                "pids": pids,
                "main_pid": pids[0] if pids else (9999 if self._dev_simulated_running else None),
                "is_systemd": False,
                "simulated": True,
            }

        try:
            # Check is-active
            cmd = ["systemctl", "is-active", self.service_name]
            res = subprocess.run(cmd, capture_output=True, text=True)
            state_str = res.stdout.strip()
            is_active = state_str == "active"

            # Check detailed status
            cmd_status = ["systemctl", "show", self.service_name, "--property=MainPID,ActiveState,SubState,ActiveEnterTimestamp"]
            res_status = subprocess.run(cmd_status, capture_output=True, text=True)
            props = {}
            for line in res_status.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()

            main_pid = int(props.get("MainPID", 0)) if props.get("MainPID", "0").isdigit() else 0

            return {
                "service": self.service_name,
                "is_running": is_active,
                "state": f"{props.get('ActiveState', state_str)} ({props.get('SubState', '')})",
                "main_pid": main_pid if main_pid > 0 else None,
                "active_since": props.get("ActiveEnterTimestamp", ""),
                "is_systemd": True,
                "simulated": False,
            }
        except Exception as e:
            return {
                "service": self.service_name,
                "is_running": False,
                "state": f"error: {str(e)}",
                "main_pid": None,
                "is_systemd": True,
                "simulated": False,
            }

    def start(self) -> Dict[str, Any]:
        """Start the dvmhost service."""
        log_audit("SERVICE", "START", f"Requesting start of {self.service_name}")
        if not self.has_systemctl:
            self._dev_simulated_running = True
            self._dev_simulated_start_time = time.time()
            return {"success": True, "message": "Service started (simulated dev mode)"}

        try:
            res = subprocess.run(["sudo", "systemctl", "start", self.service_name], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return {"success": True, "message": f"{self.service_name} started successfully"}
            else:
                return {"success": False, "message": res.stderr.strip() or f"systemctl start exited with code {res.returncode}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def stop(self) -> Dict[str, Any]:
        """Stop the dvmhost service."""
        log_audit("SERVICE", "STOP", f"Requesting stop of {self.service_name}")
        if not self.has_systemctl:
            self._dev_simulated_running = False
            return {"success": True, "message": "Service stopped (simulated dev mode)"}

        try:
            res = subprocess.run(["sudo", "systemctl", "stop", self.service_name], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return {"success": True, "message": f"{self.service_name} stopped successfully"}
            else:
                return {"success": False, "message": res.stderr.strip() or f"systemctl stop exited with code {res.returncode}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def restart(self) -> Dict[str, Any]:
        """Restart the dvmhost service."""
        log_audit("SERVICE", "RESTART", f"Requesting restart of {self.service_name}")
        if not self.has_systemctl:
            self._dev_simulated_running = True
            self._dev_simulated_start_time = time.time()
            return {"success": True, "message": "Service restarted (simulated dev mode)"}

        try:
            res = subprocess.run(["sudo", "systemctl", "restart", self.service_name], capture_output=True, text=True, timeout=20)
            if res.returncode == 0:
                return {"success": True, "message": f"{self.service_name} restarted successfully"}
            else:
                return {"success": False, "message": res.stderr.strip() or f"systemctl restart exited with code {res.returncode}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def verify_running(self, timeout_sec: float = 6.0) -> bool:
        """Poll service for timeout_sec to verify it remains running."""
        deadline = time.time() + timeout_sec
        time.sleep(1.0)
        while time.time() < deadline:
            status = self.get_status()
            if not status["is_running"]:
                return False
            time.sleep(0.5)
        return True

    def get_recent_logs(self, lines: int = 50) -> List[str]:
        """Retrieve recent journalctl logs for dvmhost."""
        if not self.has_systemctl:
            return [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [Nexus Dev Mode] Running in non-systemd environment. Journalctl logs unavailable."]

        try:
            cmd = ["journalctl", "-u", self.service_name, "-n", str(lines), "--no-pager"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return res.stdout.splitlines()
            return [f"journalctl exited with code {res.returncode}: {res.stderr}"]
        except Exception as e:
            return [f"Error retrieving journal logs: {str(e)}"]


service_controller = ServiceController()
