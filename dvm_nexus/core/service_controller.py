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
        self._dev_simulated_failed = False
        self._dev_simulated_start_time = None
        # Cache to reduce process-fork overhead during frequent Web UI polling
        self._cached_status: Optional[Dict[str, Any]] = None
        self._status_timestamp: float = 0.0
        self._status_ttl: float = 2.0  # seconds

    def get_status(self, force: bool = False) -> Dict[str, Any]:
        """Query service status and PID with short in-memory cache to optimize UI polling."""
        now = time.time()
        if not force and self._cached_status is not None and (now - self._status_timestamp < self._status_ttl):
            return dict(self._cached_status)

        if not self.has_systemctl:
            # Check if dvmhost process is running via psutil
            pids = []
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if "dvmhost" in p.info["name"].lower():
                        pids.append(p.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            is_running = (len(pids) > 0 or self._dev_simulated_running) and not self._dev_simulated_failed
            state_str = "active (running)" if is_running else ("failed (failed)" if self._dev_simulated_failed else "inactive (dead)")
            res = {
                "service": self.service_name,
                "is_running": is_running,
                "is_failed": self._dev_simulated_failed,
                "state": state_str,
                "pids": pids,
                "main_pid": pids[0] if pids else (9999 if is_running else None),
                "exit_code": 1 if self._dev_simulated_failed else None,
                "exit_result": "exit-code" if self._dev_simulated_failed else None,
                "is_systemd": False,
                "simulated": True,
            }
            self._cached_status = res
            self._status_timestamp = now
            return res

        try:
            # Check is-active
            cmd = ["systemctl", "is-active", self.service_name]
            res = subprocess.run(cmd, capture_output=True, text=True)
            state_str = res.stdout.strip()
            is_active = state_str == "active"

            # Check detailed status
            cmd_status = [
                "systemctl", "show", self.service_name,
                "--property=MainPID,ActiveState,SubState,ActiveEnterTimestamp,Result,ExecMainStatus,ExecMainCode"
            ]
            res_status = subprocess.run(cmd_status, capture_output=True, text=True)
            props = {}
            for line in res_status.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()

            main_pid = int(props.get("MainPID", 0)) if props.get("MainPID", "0").isdigit() else 0
            act_state = props.get("ActiveState", state_str)
            sub_state = props.get("SubState", "")
            result_str = props.get("Result", "")
            is_failed = act_state == "failed" or sub_state == "failed" or (result_str not in ("", "success") and not is_active)
            exit_code = int(props.get("ExecMainStatus", 0)) if props.get("ExecMainStatus", "0").isdigit() else None

            status_dict = {
                "service": self.service_name,
                "is_running": is_active,
                "is_failed": is_failed,
                "state": f"{act_state} ({sub_state})",
                "main_pid": main_pid if main_pid > 0 else None,
                "exit_code": exit_code,
                "exit_result": result_str,
                "active_since": props.get("ActiveEnterTimestamp", ""),
                "is_systemd": True,
                "simulated": False,
            }
            self._cached_status = status_dict
            self._status_timestamp = now
            return status_dict
        except Exception as e:
            err_dict = {
                "service": self.service_name,
                "is_running": False,
                "is_failed": True,
                "state": f"error: {str(e)}",
                "main_pid": None,
                "exit_code": None,
                "exit_result": "error",
                "is_systemd": True,
                "simulated": False,
            }
            self._cached_status = err_dict
            self._status_timestamp = now
            return err_dict

    def start(self) -> Dict[str, Any]:
        """Start the dvmhost service."""
        self._cached_status = None
        self._status_timestamp = 0.0
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
                err = res.stderr.strip() or f"systemctl start exited with code {res.returncode}"
                return {
                    "success": False,
                    "message": err,
                    "recent_logs": self.get_recent_logs(lines=10),
                }
        except Exception as e:
            return {"success": False, "message": str(e), "recent_logs": [str(e)]}

    def stop(self) -> Dict[str, Any]:
        """Stop the dvmhost service."""
        self._cached_status = None
        self._status_timestamp = 0.0
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
        self._cached_status = None
        self._status_timestamp = 0.0
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
                err = res.stderr.strip() or f"systemctl restart exited with code {res.returncode}"
                return {
                    "success": False,
                    "message": err,
                    "recent_logs": self.get_recent_logs(lines=10),
                }
        except Exception as e:
            return {"success": False, "message": str(e), "recent_logs": [str(e)]}

    def verify_running(self, timeout_sec: float = 6.0) -> bool:
        """Poll service for timeout_sec to verify it remains running."""
        deadline = time.time() + timeout_sec
        time.sleep(1.0)
        while time.time() < deadline:
            status = self.get_status(force=True)
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
