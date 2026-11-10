"""
DVM Nexus - System & Service Diagnostics Engine
Performs live health checks, crash log analysis, and automated configuration repair for DVMHost.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import yaml

from dvm_nexus.core.config import settings, DEFAULT_CONFIGS_DIR
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.database import log_audit


class DiagnosticsEngine:
    """Automated health checker and diagnostic suite for DVMHost and repeater services."""

    def __init__(self):
        self.dvm_dir = settings.dvm_dir
        self.service_controller = service_controller

    def get_service_logs(self, service_name: str = "dvmhost", lines: int = 60, filter_level: str = "all") -> Dict[str, Any]:
        """
        Fetch recent journalctl logs for a service and tag severity levels.
        filter_level can be 'all' or 'errors'.
        """
        full_service = service_name if service_name.endswith(".service") else f"{service_name}.service"

        has_systemctl = shutil.which("journalctl") is not None and os.name != "nt"
        raw_lines: List[str] = []

        if not has_systemctl:
            # Simulated logs for development / testing environment
            status = self.service_controller.get_status()
            now_str = time.strftime("%b %d %H:%M:%S")
            if status.get("is_failed") or not status.get("is_running"):
                raw_lines = [
                    f"{now_str} nexus-pi5 systemd[1]: Starting DVMHost Digital Voice Modem Host...",
                    f"{now_str} nexus-pi5 dvmhost[1234]: DVMHost v1.0.0-git (Built for Raspberry Pi 5 ARM64)",
                    f"{now_str} nexus-pi5 dvmhost[1234]: [INFO] Loading configuration from /opt/dvm/config.yml",
                    f"{now_str} nexus-pi5 dvmhost[1234]: [WARN] System identity ABCD123 is using default demonstration ID",
                    f"{now_str} nexus-pi5 dvmhost[1234]: [ERROR] iAgreeNotToBeStupid check failed! You must agree not to be stupid!",
                    f"{now_str} nexus-pi5 dvmhost[1234]: [FATAL] Fatal configuration violation encountered. Aborting startup.",
                    f"{now_str} nexus-pi5 systemd[1]: dvmhost.service: Main process exited, code=exited, status=1/FAILURE",
                    f"{now_str} nexus-pi5 systemd[1]: dvmhost.service: Failed with result 'exit-code'.",
                    f"{now_str} nexus-pi5 systemd[1]: Failed to start DVMHost Digital Voice Modem Host.",
                ]
            else:
                raw_lines = [
                    f"{now_str} nexus-pi5 systemd[1]: Started DVMHost Digital Voice Modem Host.",
                    f"{now_str} nexus-pi5 dvmhost[9999]: DVMHost active. Listening for RF frames on /dev/ttyAMA0 @ 115200.",
                    f"{now_str} nexus-pi5 dvmhost[9999]: [INFO] DMR Tier II Duplex controller ready. ColorCode: 1, Timeslots: 1 & 2.",
                    f"{now_str} nexus-pi5 dvmhost[9999]: [INFO] FNE Network link connected to BrandMeister DMR master.",
                ]
        else:
            try:
                cmd = ["journalctl", "-u", full_service, "-n", str(lines), "--no-pager"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
                if res.returncode == 0 and res.stdout.strip():
                    raw_lines = res.stdout.strip().splitlines()
                else:
                    raw_lines = [f"No journal logs found for {full_service}."]
            except Exception as e:
                raw_lines = [f"Failed to query journalctl: {str(e)}"]

        parsed_entries = []
        error_keywords = ("error", "fatal", "fail", "failed", "failure", "abort", "panic", "cannot", "denied", "exit-code", "exited")
        warning_keywords = ("warn", "warning", "degraded", "timeout", "retry")

        for line in raw_lines:
            line_lower = line.lower()
            level = "info"
            if any(k in line_lower for k in error_keywords):
                level = "error"
            elif any(k in line_lower for k in warning_keywords):
                level = "warn"

            if filter_level == "errors" and level not in ("error", "warn"):
                continue

            parsed_entries.append({
                "raw": line,
                "level": level,
            })

        return {
            "service": full_service,
            "total_lines": len(parsed_entries),
            "filter": filter_level,
            "entries": parsed_entries,
        }

    def run_health_checks(self) -> Dict[str, Any]:
        """
        Execute automated diagnostics across systemd, config.yml, data tables, and hardware UART.
        Returns detailed check results, overall status ('ok', 'warning', 'critical'), and auto-fix recommendations.
        """
        checks: List[Dict[str, Any]] = []
        status = self.service_controller.get_status()
        is_service_running = status.get("is_running", False)
        is_service_failed = status.get("is_failed", False)
        state_str = status.get("state", "unknown")

        # ----------------------------------------------------------------------
        # Check 1: Systemd Service Health
        # ----------------------------------------------------------------------
        if is_service_running:
            checks.append({
                "id": "service_state",
                "name": "Radio Service Status (systemd)",
                "status": "pass",
                "badge": "RUNNING",
                "summary": f"{status.get('service')} is active and running (PID: {status.get('main_pid', 'Active')}).",
                "auto_fixable": False,
            })
        elif is_service_failed:
            checks.append({
                "id": "service_state",
                "name": "Radio Service Status (systemd)",
                "status": "fail",
                "badge": "FAILED",
                "summary": f"{status.get('service')} crashed with state: '{state_str}'. Control process exited with error code.",
                "remedy": "Inspect the crash logs below and click 'Auto-Fix Configuration & Restart'.",
                "auto_fixable": True,
                "fix_id": "auto_repair",
            })
        else:
            checks.append({
                "id": "service_state",
                "name": "Radio Service Status (systemd)",
                "status": "warn",
                "badge": "STOPPED",
                "summary": f"{status.get('service')} is currently stopped/inactive ({state_str}).",
                "remedy": "Start the daemon using the control buttons.",
                "auto_fixable": False,
            })

        # ----------------------------------------------------------------------
        # Check 2: Configuration Syntax & DVMHost Policy Flags
        # ----------------------------------------------------------------------
        cfg_dict: Dict[str, Any] = {}
        cfg_path = dvm_config_manager.config_path

        if not cfg_path.exists():
            checks.append({
                "id": "config_file",
                "name": "Configuration File (config.yml)",
                "status": "fail",
                "badge": "MISSING",
                "summary": f"Target file '{cfg_path}' does not exist on disk.",
                "remedy": "Click Auto-Fix to generate baseline configuration files.",
                "auto_fixable": True,
                "fix_id": "restore_baseline_config",
            })
        else:
            try:
                cfg_dict = dvm_config_manager.load_config_dict()
                checks.append({
                    "id": "config_file",
                    "name": "Configuration File Syntax",
                    "status": "pass",
                    "badge": "VALID YAML",
                    "summary": "config.yml is present and passed YAML structural parsing.",
                    "auto_fixable": False,
                })
            except Exception as e:
                checks.append({
                    "id": "config_file",
                    "name": "Configuration File Syntax",
                    "status": "fail",
                    "badge": "SYNTAX ERROR",
                    "summary": f"Failed to parse config.yml: {str(e)}",
                    "remedy": "Correct YAML formatting syntax or restore a previous backup from Config & History.",
                    "auto_fixable": False,
                })

        # Check iAgreeNotToBeStupid policy flag
        if cfg_dict:
            agree = cfg_dict.get("iAgreeNotToBeStupid")
            if agree is True:
                checks.append({
                    "id": "stupid_check",
                    "name": "DVMHost Policy: iAgreeNotToBeStupid",
                    "status": "pass",
                    "badge": "AGREED",
                    "summary": "iAgreeNotToBeStupid is enabled (required by DVMHost core startup).",
                    "auto_fixable": False,
                })
            else:
                checks.append({
                    "id": "stupid_check",
                    "name": "DVMHost Policy: iAgreeNotToBeStupid",
                    "status": "fail",
                    "badge": "FAILED (ABORT)",
                    "summary": "iAgreeNotToBeStupid is false. DVMHost aborts immediately on launch if this is not true.",
                    "remedy": "Must be set to true in config.yml.",
                    "auto_fixable": True,
                    "fix_id": "enable_agree_policy",
                })

            # Check daemon mode (under systemd Type=simple, daemon should be false)
            daemon_val = cfg_dict.get("daemon")
            if daemon_val is False:
                checks.append({
                    "id": "daemon_mode",
                    "name": "Process Mode (daemon: false)",
                    "status": "pass",
                    "badge": "FOREGROUND",
                    "summary": "Daemon mode is disabled, allowing systemd to properly monitor and log the process.",
                    "auto_fixable": False,
                })
            else:
                checks.append({
                    "id": "daemon_mode",
                    "name": "Process Mode (daemon: true)",
                    "status": "warn",
                    "badge": "FORKING RISKY",
                    "summary": "daemon: true is enabled. Under systemd Type=simple, forking causes systemd to assume the control process crashed.",
                    "remedy": "Set 'daemon: false' in config.yml so systemd directly manages the process.",
                    "auto_fixable": True,
                    "fix_id": "set_daemon_false",
                })

        # ----------------------------------------------------------------------
        # Check 3: Modem Serial Port & Linux Hardware UART
        # ----------------------------------------------------------------------
        uart_port = cfg_dict.get("system", {}).get("modem", {}).get("protocol", {}).get("uart", {}).get("port", "/dev/ttyAMA0")
        if os.name != "nt":
            if os.path.exists(uart_port):
                checks.append({
                    "id": "modem_port",
                    "name": f"Modem Serial Port ({uart_port})",
                    "status": "pass",
                    "badge": "FOUND",
                    "summary": f"Device node {uart_port} exists on the system.",
                    "auto_fixable": False,
                })
            else:
                checks.append({
                    "id": "modem_port",
                    "name": f"Modem Serial Port ({uart_port})",
                    "status": "fail",
                    "badge": "NOT FOUND",
                    "summary": f"Device {uart_port} does not exist. If using an MMDVM Duplex Hat on Pi 5 GPIO pins, GPIO UART (/dev/ttyAMA0) must be enabled.",
                    "remedy": "Run 'sudo /opt/dvm-nexus/scripts/setup-pi5-uart.sh' and reboot.",
                    "auto_fixable": False,
                })

            # Check for serial-getty console lock on ttyAMA0
            try:
                getty_check = subprocess.run(["systemctl", "is-active", "serial-getty@ttyAMA0.service"], capture_output=True, text=True)
                if getty_check.stdout.strip() == "active":
                    checks.append({
                        "id": "serial_getty",
                        "name": "Serial Console Conflict (ttyAMA0)",
                        "status": "fail",
                        "badge": "LOCKED",
                        "summary": "Linux serial login console (serial-getty@ttyAMA0) is active and locking /dev/ttyAMA0.",
                        "remedy": "Disable serial-getty via 'sudo /opt/dvm-nexus/scripts/setup-pi5-uart.sh'.",
                        "auto_fixable": False,
                    })
            except Exception:
                pass
        else:
            checks.append({
                "id": "modem_port",
                "name": f"Modem Serial Port ({uart_port})",
                "status": "pass",
                "badge": "DEV SIM",
                "summary": f"Configured for {uart_port} (simulated on Windows host).",
                "auto_fixable": False,
            })

        # ----------------------------------------------------------------------
        # Check 4: Data Tables & ACLs
        # ----------------------------------------------------------------------
        for dat_name in ("iden_table.dat", "rid_acl.dat", "talkgroup_rules.yml"):
            dat_path = self.dvm_dir / dat_name
            if dat_path.exists() and dat_path.stat().st_size > 0:
                checks.append({
                    "id": f"table_{dat_name}",
                    "name": f"Data Table: {dat_name}",
                    "status": "pass",
                    "badge": "READY",
                    "summary": f"File '{dat_name}' is present ({dat_path.stat().st_size} bytes).",
                    "auto_fixable": False,
                })
            else:
                checks.append({
                    "id": f"table_{dat_name}",
                    "name": f"Data Table: {dat_name}",
                    "status": "fail",
                    "badge": "MISSING",
                    "summary": f"File '{dat_name}' is missing or empty in {self.dvm_dir}.",
                    "remedy": f"Restore '{dat_name}' from baseline templates.",
                    "auto_fixable": True,
                    "fix_id": f"restore_table_{dat_name}",
                })

        # Overall health calculation
        has_fails = any(c["status"] == "fail" for c in checks)
        has_warns = any(c["status"] == "warn" for c in checks)
        overall_status = "critical" if has_fails else ("warning" if has_warns else "healthy")

        auto_fix_count = sum(1 for c in checks if c.get("auto_fixable", False))

        return {
            "overall_status": overall_status,
            "has_fails": has_fails,
            "has_warns": has_warns,
            "auto_fix_count": auto_fix_count,
            "checks": checks,
            "service_status": status,
        }

    def auto_fix_all(self) -> Dict[str, Any]:
        """
        Automatically repair detected configuration discrepancies and attempt service restart.
        """
        fixes_applied: List[str] = []

        try:
            # 1. Enforce config.yml parameters (iAgreeNotToBeStupid=True, daemon=False)
            cfg = dvm_config_manager.load_config_dict()
            modified = False

            if cfg.get("iAgreeNotToBeStupid") is not True:
                cfg["iAgreeNotToBeStupid"] = True
                fixes_applied.append("Set 'iAgreeNotToBeStupid: true' in config.yml")
                modified = True

            if cfg.get("daemon") is not False:
                cfg["daemon"] = False
                fixes_applied.append("Set 'daemon: false' in config.yml (systemd foreground supervision)")
                modified = True

            if modified:
                dvm_config_manager.save_config_dict(cfg, summary="Auto-Fix: Corrected DVMHost policy and daemon flags")

            # 2. Restore any missing baseline data tables
            for tbl in ("iden_table.dat", "rid_acl.dat", "talkgroup_rules.yml"):
                target = self.dvm_dir / tbl
                if not target.exists() or target.stat().st_size == 0:
                    src_example = DEFAULT_CONFIGS_DIR / f"{Path(tbl).stem}.example{Path(tbl).suffix}"
                    if src_example.exists():
                        backup_manager.atomic_write(target, src_example.read_text(encoding="utf-8"))
                        fixes_applied.append(f"Restored missing data table '{tbl}' from template")

            # 3. Trigger restart of dvmhost service
            restart_res = self.service_controller.restart()
            time.sleep(1.5)
            new_status = self.service_controller.get_status()

            is_running = new_status.get("is_running", False)
            log_audit("DIAGNOSTICS", "AUTO_FIX", f"Auto-fix applied {len(fixes_applied)} remedies. Service running={is_running}")

            return {
                "success": is_running,
                "fixes_applied": fixes_applied,
                "service_state": new_status.get("state"),
                "is_running": is_running,
                "message": (
                    f"Applied {len(fixes_applied)} automatic fixes. DVMHost is now RUNNING!"
                    if is_running else
                    f"Applied {len(fixes_applied)} fixes, but service did not stay running ({restart_res.get('message')}). Check crash logs below."
                )
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "fixes_applied": fixes_applied,
                "message": f"Auto-fix encountered an error: {str(e)}"
            }


diagnostics_engine = DiagnosticsEngine()
