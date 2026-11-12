"""
DVM Nexus - System & Service Diagnostics Engine
Performs live health checks, crash log analysis, and automated configuration repair for DVMHost.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import yaml

from dvm_nexus.core.config import settings, DEFAULT_CONFIGS_DIR
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.dvm_yaml import (
    dvm_config_manager, is_banned_p25_wacn, sanitize_p25_wacn, parse_channel_no, format_channel_no
)
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.database import log_audit


class DiagnosticsEngine:
    """Automated health checker and diagnostic suite for DVMHost and repeater services."""

    def __init__(self):
        self.dvm_dir = settings.dvm_dir
        self.service_controller = service_controller
        self._simulated_cleared = False
        self._cached_report: Optional[Dict[str, Any]] = None
        self._cached_report_time: float = 0.0
        self._cached_modem_summary: Optional[Dict[str, Any]] = None
        self._cached_modem_summary_time: float = 0.0

    def get_modem_firmware_summary(self, cfg_dict: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
        """
        Inspect config.yml and recent dvmhost startup logs to verify:
        - modem.protocol.type == 'uart', mode == 'air', port == '/dev/ttyAMA0', speed == 115200
        - DVMProject firmware (dvmfirmware-hs: 'Digital Voice Modem') vs stock MMDVM_HS firmware
        - 1-antenna [MMDVM_HS_Hat] vs 2-antenna [MMDVM_HS_Dual_Hat] duplex mismatch
        - /dev/ttyAMA0 lock by Bluetooth (hciuart) or serial-getty@ttyAMA0
        """
        now = time.time()
        if not force and self._cached_modem_summary is not None and (now - self._cached_modem_summary_time < 4.0):
            return dict(self._cached_modem_summary)

        if cfg_dict is None:
            try:
                cfg_dict = dvm_config_manager.load_config_dict()
            except Exception:
                cfg_dict = {}

        sys_blk = cfg_dict.get("system", {}) if isinstance(cfg_dict, dict) else {}
        config_duplex = bool(sys_blk.get("duplex", False))
        modem_blk = sys_blk.get("modem", {}) if isinstance(sys_blk, dict) else {}
        proto_blk = modem_blk.get("protocol", {}) if isinstance(modem_blk, dict) else {}
        uart_blk = proto_blk.get("uart", {}) if isinstance(proto_blk, dict) else {}

        protocol_type = str(proto_blk.get("type", "uart")).strip()
        protocol_mode = str(proto_blk.get("mode", "air")).strip()
        uart_port = str(uart_blk.get("port", "/dev/ttyAMA0")).strip()
        try:
            uart_speed = int(uart_blk.get("speed", 115200))
        except (ValueError, TypeError):
            uart_speed = 115200

        # Inspect recent journalctl logs for (MODEM) lines
        log_data = self.get_service_logs("dvmhost", lines=120, filter_level="all")
        raw_lines = [e.get("raw", "") for e in log_data.get("entries", [])]

        firmware_string = ""
        modem_ready_mode = ""
        is_dvm_firmware: Optional[bool] = None
        is_single_hat_firmware = False
        is_dual_hat_firmware = False
        uart_timeout_in_logs = False

        for line in raw_lines:
            if "(MODEM)" in line or "MMDVM" in line:
                if "Digital Voice Modem" in line:
                    is_dvm_firmware = True
                    parts = line.split("(MODEM)", 1)
                    firmware_string = parts[1].strip() if len(parts) > 1 else line.strip()
                    if "[MMDVM_HS_Hat]" in firmware_string and "Dual" not in firmware_string:
                        is_single_hat_firmware = True
                    elif "Dual" in firmware_string:
                        is_dual_hat_firmware = True
                elif ("MMDVM_HS" in line or "ZUMspot" in line) and "Digital Voice Modem" not in line:
                    if is_dvm_firmware is None:
                        is_dvm_firmware = False
                        parts = line.split("(MODEM)", 1)
                        firmware_string = parts[1].strip() if len(parts) > 1 else line.strip()
                if "Modem Ready" in line:
                    m = re.search(r"Modem Ready \[([^\]]+)\]", line)
                    if m:
                        modem_ready_mode = m.group(1)
                if "Unable to read the firmware version" in line or "Cannot open device" in line or "Unable to open the modem" in line:
                    uart_timeout_in_logs = True

        serial_getty_active = False
        bt_uart_active = False
        if os.name != "nt":
            try:
                g_chk = subprocess.run(["systemctl", "is-active", "serial-getty@ttyAMA0.service"], capture_output=True, text=True, timeout=1.0)
                serial_getty_active = (g_chk.stdout.strip() == "active")
            except Exception:
                pass
            try:
                bt_chk = subprocess.run(["systemctl", "is-active", "hciuart.service"], capture_output=True, text=True, timeout=1.0)
                bt_uart_active = (bt_chk.stdout.strip() == "active")
            except Exception:
                pass

        warnings: List[str] = []
        if protocol_type.lower() != "uart" or protocol_mode.lower() != "air":
            warnings.append(
                f"Modem protocol is set to type='{protocol_type}', mode='{protocol_mode}' (expected type='uart', mode='air')."
            )
        if uart_speed != 115200:
            warnings.append(
                f"UART baud rate is set to {uart_speed} (DVMProject dvmfirmware-hs uses 115200 bps)."
            )
        if is_dvm_firmware is False:
            warnings.append(
                f"Stock MMDVM_HS firmware detected ({firmware_string}). DVMHost requires DVMProject firmware (dvmfirmware-hs, Protocol 04) to link DMR and P25 radios."
            )
        if is_single_hat_firmware and config_duplex:
            warnings.append(
                "Hardware Duplex Mismatch: Your MMDVM hat reports 1-antenna [MMDVM_HS_Hat] firmware, which cannot transmit and receive simultaneously. Switch to Simplex (duplex: false, RX = TX) or use a 2-antenna [MMDVM_HS_Dual_Hat] board."
            )
        if serial_getty_active:
            warnings.append(
                "Linux login console (serial-getty@ttyAMA0.service) is active and blocking /dev/ttyAMA0."
            )
        if bt_uart_active:
            warnings.append(
                "Bluetooth UART service (hciuart.service) is active and may conflict with /dev/ttyAMA0 unless dtoverlay=disable-bt is set."
            )
        if uart_timeout_in_logs:
            warnings.append(
                f"DVMHost reported a serial communication failure on {uart_port} ('Unable to read the firmware version'). Check GPIO UART and hat seating."
            )

        summary = {
            "protocol_type": protocol_type,
            "protocol_mode": protocol_mode,
            "uart_port": uart_port,
            "uart_speed": uart_speed,
            "config_duplex": config_duplex,
            "firmware_string": firmware_string or "DVMProject dvmfirmware-hs (awaiting log line)",
            "is_dvm_firmware": is_dvm_firmware,
            "is_single_hat_firmware": is_single_hat_firmware,
            "is_dual_hat_firmware": is_dual_hat_firmware,
            "modem_ready_mode": modem_ready_mode,
            "serial_getty_active": serial_getty_active,
            "bt_uart_active": bt_uart_active,
            "uart_timeout_in_logs": uart_timeout_in_logs,
            "warnings": warnings,
            "healthy": len(warnings) == 0,
        }
        self._cached_modem_summary = summary
        self._cached_modem_summary_time = now
        return summary

    def clear_logs(self, service_name: str = "dvmhost") -> Dict[str, Any]:
        """
        Clear or vacuum systemd journal logs, or reset the terminal stream.
        """
        has_systemctl = shutil.which("journalctl") is not None and os.name != "nt"
        if not has_systemctl:
            self._simulated_cleared = True
            log_audit("DIAGNOSTICS", "CLEAR_LOGS", f"Cleared terminal logs for {service_name} (simulated)")
            return {"success": True, "message": "Terminal log stream cleared successfully."}

        try:
            # Rotate journal and vacuum immediately
            subprocess.run(["sudo", "journalctl", "--rotate"], capture_output=True, timeout=5)
            res = subprocess.run(["sudo", "journalctl", "--vacuum-time=1s"], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                log_audit("DIAGNOSTICS", "CLEAR_LOGS", f"Cleared journal logs for {service_name}")
                return {"success": True, "message": "Journal logs flushed and vacuumed successfully."}
            else:
                return {"success": False, "message": res.stderr.strip() or "Failed to vacuum journal"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_service_logs(self, service_name: str = "dvmhost", lines: int = 60, filter_level: str = "all") -> Dict[str, Any]:
        """
        Fetch recent journalctl logs for a service and tag severity levels.
        filter_level can be 'all' or 'errors'.
        """
        full_service = service_name if service_name.endswith(".service") else f"{service_name}.service"

        has_systemctl = shutil.which("journalctl") is not None and os.name != "nt"
        raw_lines: List[str] = []

        if not has_systemctl:
            now_str = time.strftime("%b %d %H:%M:%S")
            if self._simulated_cleared:
                raw_lines = [f"{now_str} nexus-pi5 [INFO] Journal log stream cleared. Waiting for new service events..."]
            else:
                # Simulated logs for development / testing environment
                status = self.service_controller.get_status()
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

            # Check for DVMHost standard log level indicators first:
            # "I:" / "[INFO]" = Informational (e.g. "I:     Ignore Data PDU CRC Error: no")
            # "E:" / "[ERROR]" / "[FATAL]" = Real Errors
            # "W:" / "[WARN]" = Warnings
            # "D:" / "[DEBUG]" = Debug
            if ": i:" in line_lower or "[info]" in line_lower:
                level = "info"
            elif ": e:" in line_lower or "[error]" in line_lower or "[fatal]" in line_lower:
                level = "error"
            elif ": w:" in line_lower or "[warn]" in line_lower or "[warning]" in line_lower:
                level = "warn"
            elif ": d:" in line_lower or "[debug]" in line_lower:
                level = "info"
            elif any(k in line_lower for k in error_keywords):
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

    def invalidate_cache(self) -> None:
        """Clear cached health check report."""
        self._cached_report = None
        self._cached_report_time = 0.0
        self._cached_modem_summary = None
        self._cached_modem_summary_time = 0.0

    def run_health_checks(self, force: bool = False) -> Dict[str, Any]:
        """
        Execute automated diagnostics across systemd, config.yml, data tables, and hardware UART.
        Returns detailed check results, overall status ('ok', 'warning', 'critical'), and auto-fix recommendations.
        Cached for up to 5 seconds to prevent event-loop lockups during concurrent HTMX polling bursts.
        """
        now = time.time()
        cfg_path = dvm_config_manager.config_path
        cfg_mtime = 0.0
        try:
            if cfg_path.exists():
                cfg_mtime = cfg_path.stat().st_mtime
        except Exception:
            pass

        last_mtime = getattr(self, "_last_config_mtime", 0.0)
        if not force and self._cached_report is not None and (now - self._cached_report_time < 5.0) and (cfg_mtime == last_mtime):
            return dict(self._cached_report)

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
            # Check yaml-cpp parser compatibility (sequences must be indented relative to mapping key)
            try:
                raw_cfg = cfg_path.read_text(encoding="utf-8")
                bad_indent = False
                for m in re.finditer(r'^([ \t]*)([a-zA-Z0-9_-]+):\s*\n([ \t]*)-\s', raw_cfg, re.MULTILINE):
                    if len(m.group(3)) <= len(m.group(1)):
                        bad_indent = True
                        break
                if bad_indent:
                    checks.append({
                        "id": "yaml_indentation",
                        "name": "YAML-CPP Compatibility (List Indentation)",
                        "status": "fail",
                        "badge": "BAD INDENT",
                        "summary": "config.yml has list entries not indented under their parent key. DVMHost yaml-cpp parser will reject this with 'Different entry is not allowed'.",
                        "remedy": "Click Auto-Repair to automatically format sequences with compliant indentation.",
                        "auto_fixable": True,
                        "fix_id": "fix_yaml_indent",
                    })
                else:
                    checks.append({
                        "id": "yaml_indentation",
                        "name": "YAML-CPP Compatibility (List Indentation)",
                        "status": "pass",
                        "badge": "COMPLIANT",
                        "summary": "List indentation is fully compliant with DVMHost C++ yaml-cpp parser.",
                        "auto_fixable": False,
                    })
            except Exception:
                pass

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

            # Check logging configuration & target directory
            log_conf = cfg_dict.get("log", {})
            use_syslog = log_conf.get("useSyslog", False)
            file_path = str(log_conf.get("filePath", "."))

            if not use_syslog and file_path in (".", ""):
                checks.append({
                    "id": "logging_config",
                    "name": "Logging Target & Permissions",
                    "status": "fail",
                    "badge": "UNSAFE PATH",
                    "summary": "log.useSyslog is false and filePath is relative ('.'). DVMHost may crash with 'FATAL PANIC; unable to open the log file'.",
                    "remedy": "Click Auto-Repair to enable syslog integration and set log directory to /opt/dvm.",
                    "auto_fixable": True,
                    "fix_id": "fix_logging_config",
                })
            else:
                checks.append({
                    "id": "logging_config",
                    "name": "Logging Target & Permissions",
                    "status": "pass",
                    "badge": "JOURNAL READY",
                    "summary": "Logging is directed to systemd journal and configured safely.",
                    "auto_fixable": False,
                })

        # ----------------------------------------------------------------------
        # Check 3: Modem Serial Port & Linux Hardware UART
        # ----------------------------------------------------------------------
        modem_proto_type = cfg_dict.get("system", {}).get("modem", {}).get("protocol", {}).get("type", "uart")
        modem_proto_mode = cfg_dict.get("system", {}).get("modem", {}).get("protocol", {}).get("mode", "air")
        uart_port = cfg_dict.get("system", {}).get("modem", {}).get("protocol", {}).get("uart", {}).get("port", "/dev/ttyAMA0")
        uart_speed = cfg_dict.get("system", {}).get("modem", {}).get("protocol", {}).get("uart", {}).get("speed", 115200)
        modem_summary = self.get_modem_firmware_summary(cfg_dict=cfg_dict)

        # Safety Check: Null Dummy Modem Trap & Protocol Mode
        if str(modem_proto_type).lower() in ("null", "'null'", '"null"'):
            checks.append({
                "id": "modem_null_trap",
                "name": "Modem Controller Hardware Type",
                "status": "fail",
                "badge": "DUMMY SIMULATOR",
                "summary": "Modem protocol is set to 'null'! DVMHost is running in dummy software simulation mode and disconnected from the physical radio hat.",
                "remedy": "Click Auto-Repair to switch to hardware UART mode (/dev/ttyAMA0).",
                "auto_fixable": True,
                "fix_id": "fix_modem_null",
            })
        elif str(modem_proto_mode).lower() != "air" or int(uart_speed or 115200) != 115200:
            checks.append({
                "id": "modem_null_trap",
                "name": "Modem Protocol & UART Speed",
                "status": "warn",
                "badge": "MISMATCH",
                "summary": f"Modem configured as mode='{modem_proto_mode}', speed={uart_speed}. DVMProject MMDVM hats expect mode='air' at 115200 bps.",
                "remedy": "Click Auto-Repair to enforce protocol.mode: air and speed: 115200.",
                "auto_fixable": True,
                "fix_id": "fix_modem_null",
            })
        else:
            checks.append({
                "id": "modem_null_trap",
                "name": "Modem Controller Hardware Type",
                "status": "pass",
                "badge": "AIR / 115200",
                "summary": f"Modem interface is configured for hardware '{modem_proto_type}' (mode: {modem_proto_mode}, {uart_port} @ {uart_speed} bps).",
                "auto_fixable": False,
            })

        # Safety Check: Modem Firmware & 1-Antenna vs 2-Antenna Duplex Match
        if modem_summary.get("is_dvm_firmware") is False:
            checks.append({
                "id": "modem_firmware_check",
                "name": "Modem DSP Firmware (dvmfirmware-hs)",
                "status": "fail",
                "badge": "STOCK MMDVM_HS",
                "summary": f"Stock MMDVM_HS firmware detected ({modem_summary.get('firmware_string')}). DVMHost requires DVMProject firmware (dvmfirmware-hs).",
                "remedy": "Flash DVMProject dvmfirmware-hs to the MMDVM hat.",
                "auto_fixable": False,
            })
        elif modem_summary.get("is_single_hat_firmware") and modem_summary.get("config_duplex"):
            checks.append({
                "id": "modem_firmware_check",
                "name": "Modem Hardware Duplex Capability",
                "status": "warn",
                "badge": "1-ANTENNA HAT IN DUPLEX",
                "summary": f"Modem reports 1-antenna [MMDVM_HS_Hat] firmware ({modem_summary.get('firmware_string')}), which only supports Simplex, while config.yml has duplex: true.",
                "remedy": "Switch to Simplex (Direct / Hotspot) mode on the DMR or P25 page so duplex: false and RX = TX.",
                "auto_fixable": False,
            })
        elif modem_summary.get("is_dvm_firmware") is True:
            checks.append({
                "id": "modem_firmware_check",
                "name": "Modem DSP Firmware (dvmfirmware-hs)",
                "status": "pass",
                "badge": "DVMFIRMWARE-HS",
                "summary": f"Verified DVMProject firmware: {modem_summary.get('firmware_string')}",
                "auto_fixable": False,
            })

        # Safety Check: Voice Channel RPC Port Collision & Authoritative Master Mode
        sys_cfg = cfg_dict.get("system", {}).get("config", {})
        main_rpc_port = cfg_dict.get("network", {}).get("rpcPort", 9890)
        has_rpc_collision = False
        if isinstance(sys_cfg, dict) and "voiceChNo" in sys_cfg and isinstance(sys_cfg["voiceChNo"], list):
            for vc in sys_cfg["voiceChNo"]:
                if isinstance(vc, dict) and vc.get("rpcPort") == main_rpc_port and main_rpc_port != 0:
                    has_rpc_collision = True
                    break

        if has_rpc_collision:
            checks.append({
                "id": "vc_rpc_collision",
                "name": "Voice Channel RPC Conflict",
                "status": "fail",
                "badge": "PORT CONFLICT",
                "summary": f"Voice Channel RPC port ({main_rpc_port}) collides with DVMHost main RPC port. Causes 'Detected possible misconfiguration!' warning.",
                "remedy": "Click Auto-Repair to set Voice Channel RPC port to 0 (internal standalone mode).",
                "auto_fixable": True,
                "fix_id": "fix_vc_rpc",
            })
        else:
            checks.append({
                "id": "vc_rpc_collision",
                "name": "Voice Channel RPC Configuration",
                "status": "pass",
                "badge": "RPC CLEAN",
                "summary": "Voice channel RPC bindings do not collide with main DVMHost RPC daemon.",
                "auto_fixable": False,
            })

        # Safety Check: Non-Authoritative Master Warning
        if isinstance(sys_cfg, dict) and sys_cfg.get("authoritative") is False:
            checks.append({
                "id": "authoritative_mode",
                "name": "Site Controller Master Authority",
                "status": "warn",
                "badge": "SUBORDINATE",
                "summary": "Host is non-authoritative! Requires an external secondary site controller. For single hotspot/repeater operation, authoritative must be true.",
                "remedy": "Click Auto-Repair to enable authoritative master mode.",
                "auto_fixable": True,
                "fix_id": "fix_authoritative",
            })
        else:
            checks.append({
                "id": "authoritative_mode",
                "name": "Site Controller Master Authority",
                "status": "pass",
                "badge": "AUTHORITATIVE",
                "summary": "DVMHost is running in Authoritative Master mode.",
                "auto_fixable": False,
            })

        # Safety Check: Banned P25 WACN (0xBEE00..0xBEEFF -> FATAL PANIC; error 4 / error 38258)
        raw_net_id = sys_cfg.get("netId", "BB800") if isinstance(sys_cfg, dict) else "BB800"
        if is_banned_p25_wacn(raw_net_id):
            checks.append({
                "id": "p25_wacn_panic",
                "name": "P25 WACN / NetId Validation (error 4 guard)",
                "status": "fail",
                "badge": "FATAL ERROR 4",
                "summary": f"system.config.netId is set to '{raw_net_id}'. DVMHost aborts unconditionally at startup with 'FATAL PANIC; error 4' / 'error 38258' if netId is in 0xBEE00–0xBEEFF (even in DMR mode!).",
                "remedy": "Click Auto-Repair to replace netId with standard 'BB800'.",
                "auto_fixable": True,
                "fix_id": "fix_wacn_panic",
            })
        else:
            checks.append({
                "id": "p25_wacn_panic",
                "name": "P25 WACN / NetId Validation (error 4 guard)",
                "status": "pass",
                "badge": f"WACN {raw_net_id}",
                "summary": f"system.config.netId ('{raw_net_id}') is valid and compliant with DVMHost startup checks.",
                "auto_fixable": False,
            })

        # Safety Check: Hexadecimal Channel Number (channelNo)
        raw_ch_no = sys_cfg.get("channelNo", "1") if isinstance(sys_cfg, dict) else "1"
        expected_hex_ch = format_channel_no(parse_channel_no(raw_ch_no))
        if not isinstance(raw_ch_no, str) or raw_ch_no != expected_hex_ch:
            checks.append({
                "id": "channel_no_hex",
                "name": "RF Channel Number Hex Format (channelNo)",
                "status": "fail",
                "badge": "DECIMAL OVERFLOW",
                "summary": f"channelNo is '{raw_ch_no}' instead of hex '{expected_hex_ch}'. DVMHost parses channelNo in base-16 and clamps values > $0FFF to 4095.",
                "remedy": f"Click Auto-Repair to convert channelNo to hexadecimal '{expected_hex_ch}'.",
                "auto_fixable": True,
                "fix_id": "fix_channel_no_hex",
            })
        else:
            checks.append({
                "id": "channel_no_hex",
                "name": "RF Channel Number Hex Format (channelNo)",
                "status": "pass",
                "badge": f"CH ${expected_hex_ch}",
                "summary": f"RF channelNo is formatted in hexadecimal (${expected_hex_ch}) for DVMHost.",
                "auto_fixable": False,
            })
        if os.name != "nt":
            if os.path.exists(uart_port):
                checks.append({
                    "id": "modem_port",
                    "name": f"Modem Serial Port ({uart_port})",
                    "status": "pass",
                    "badge": "PORT PRESENT",
                    "summary": f"Linux serial port '{uart_port}' exists. Note: A present port file does not confirm the MMDVM chip is answering—if DVMHost cannot read firmware, verify Pi 5 GPIO UART (dtparam=uart0=on) or USB connection.",
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
                        "remedy": "Click Auto-Repair to disable serial-getty.",
                        "auto_fixable": True,
                        "fix_id": "disable_serial_getty",
                    })
            except Exception:
                pass

            # Check Raspberry Pi boot UART configuration
            fw_dir = Path("/boot/firmware") if Path("/boot/firmware").is_dir() else Path("/boot")
            cfg_txt = fw_dir / "config.txt"
            if cfg_txt.exists():
                try:
                    c_txt = cfg_txt.read_text(encoding="utf-8", errors="ignore")
                    has_uart = "dtparam=uart0=on" in c_txt or "dtoverlay=uart0" in c_txt or "enable_uart=1" in c_txt
                    if not has_uart:
                        checks.append({
                            "id": "pi_uart_boot",
                            "name": "Raspberry Pi GPIO UART (config.txt)",
                            "status": "fail",
                            "badge": "BOOT DISABLED",
                            "summary": "GPIO UART is not enabled in /boot/firmware/config.txt. Pins 14/15 cannot talk to MMDVM hat until enabled.",
                            "remedy": "Click Auto-Repair to enable dtparam=uart0=on automatically.",
                            "auto_fixable": True,
                            "fix_id": "fix_boot_uart",
                        })
                    else:
                        checks.append({
                            "id": "pi_uart_boot",
                            "name": "Raspberry Pi GPIO UART (config.txt)",
                            "status": "pass",
                            "badge": "BOOT ENABLED",
                            "summary": "GPIO UART parameters (uart0) are configured in /boot/firmware/config.txt.",
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

        # Check iden_table.dat for 0.0 MHz offset
        iden_path = self.dvm_dir / "iden_table.dat"
        if iden_path.exists() and iden_path.stat().st_size > 0:
            try:
                from dvm_nexus.config_engine.iden_calculator import read_iden_table_file
                bad_chans = [str(e.channel_id) for e in read_iden_table_file(iden_path) if abs(e.offset_mhz) < 0.0001]
                if bad_chans:
                    checks.append({
                        "id": "iden_zero_offset",
                        "name": "P25 IDEN Table (Invalid 0.0 MHz Offset)",
                        "status": "fail",
                        "badge": "INVALID OFFSET",
                        "summary": f"Channel Id {', '.join(bad_chans)} in iden_table.dat has an offset of 0.0 MHz. DVMHost will crash on startup with 'Channel Id has an invalid Tx offset'.",
                        "remedy": "Click Auto-Repair to automatically correct 0.0 MHz offsets to standard +5.0 MHz.",
                        "auto_fixable": True,
                        "fix_id": "fix_iden_zero_offset",
                    })
            except Exception:
                pass

        # Overall health calculation
        has_fails = any(c["status"] == "fail" for c in checks)
        has_warns = any(c["status"] == "warn" for c in checks)
        overall_status = "critical" if has_fails else ("warning" if has_warns else "healthy")

        auto_fix_count = sum(1 for c in checks if c.get("auto_fixable", False))

        report = {
            "overall_status": overall_status,
            "has_fails": has_fails,
            "has_warns": has_warns,
            "auto_fix_count": auto_fix_count,
            "checks": checks,
            "service_status": status,
        }
        self._cached_report = report
        self._cached_report_time = now
        self._last_config_mtime = cfg_mtime
        return report

    def auto_fix_all(self) -> Dict[str, Any]:
        """
        Automatically repair detected configuration discrepancies and attempt service restart.
        """
        self.invalidate_cache()
        fixes_applied: List[str] = []
        reboot_needed = False

        try:
            # 1. Ensure active config.yml exists in target directory
            dvm_config_manager.ensure_active_config_exists()
            cfg_file = dvm_config_manager.config_path
            if not cfg_file.exists() or cfg_file.stat().st_size == 0:
                dmr_src = settings.profiles_dir / "default_dmr" / "config.yml"
                if dmr_src.exists():
                    backup_manager.atomic_write(cfg_file, dmr_src.read_text(encoding="utf-8"))
                    fixes_applied.append(f"Deployed default DMR configuration to '{cfg_file}'")

            # Check if raw config has yaml-cpp incompatible sequence indentation
            raw_cfg = cfg_file.read_text(encoding="utf-8") if cfg_file.exists() else ""
            bad_indent = False
            for m in re.finditer(r'^([ \t]*)([a-zA-Z0-9_-]+):\s*\n([ \t]*)-\s', raw_cfg, re.MULTILINE):
                if len(m.group(3)) <= len(m.group(1)):
                    bad_indent = True
                    break

            if bad_indent:
                fixes_applied.append("Reformatted YAML list indentation for DVMHost yaml-cpp compatibility")
                fixed_raw = re.sub(r'(^([ \t]*)[a-zA-Z0-9_-]+:\s*\n)\2(-\s)', r'\g<1>\g<2>  \g<3>', raw_cfg, flags=re.MULTILINE)
                backup_manager.atomic_write(cfg_file, fixed_raw)

            # 2. Enforce config.yml parameters (iAgreeNotToBeStupid=True, daemon=False)
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

            # Ensure simplexSameFrequency is true if simplex is enabled
            if not cfg.get("system", {}).get("duplex", True):
                if not cfg.get("system", {}).get("simplexSameFrequency"):
                    cfg.setdefault("system", {})["simplexSameFrequency"] = True
                    fixes_applied.append("Set 'simplexSameFrequency: true' for single-frequency Simplex operation")
                    modified = True

            # Enforce hardware UART mode (cure dummy null modem trap)
            modem_block = cfg.get("system", {}).get("modem", {})
            if isinstance(modem_block, dict):
                proto_blk = modem_block.get("protocol", {})
                if isinstance(proto_blk, dict):
                    if str(proto_blk.get("type", "")).lower() in ("null", "'null'", '"null"', ""):
                        proto_blk["type"] = "uart"
                        fixes_applied.append("Switched modem protocol from 'null' dummy simulator to hardware 'uart'")
                        modified = True
                    uart_sub = proto_blk.get("uart", {})
                    if isinstance(uart_sub, dict):
                        port_str = str(uart_sub.get("port", "")).strip()
                        if not port_str or (port_str == "/dev/ttyUSB0" and os.name != "nt" and not os.path.exists("/dev/ttyUSB0")):
                            target_port = "/dev/ttyAMA0" if (os.name != "nt" and os.path.exists("/dev/ttyAMA0")) else "/dev/ttyAMA0"
                            uart_sub["port"] = target_port
                            fixes_applied.append(f"Auto-routed modem serial port to hardware GPIO '{target_port}'")
                            modified = True

            # Enforce Authoritative Master Mode, WACN error-4 guard, hex channelNo & fix Voice Channel RPC collision
            sys_config_blk = cfg.get("system", {}).get("config", {})
            if isinstance(sys_config_blk, dict):
                if sys_config_blk.get("authoritative") is not True:
                    sys_config_blk["authoritative"] = True
                    fixes_applied.append("Set 'system.config.authoritative: true' (Authoritative Site Master)")
                    modified = True

                raw_net = sys_config_blk.get("netId", "BB800")
                safe_net = sanitize_p25_wacn(raw_net)
                if str(raw_net) != safe_net:
                    sys_config_blk["netId"] = safe_net
                    fixes_applied.append(f"Replaced banned P25 WACN '{raw_net}' with '{safe_net}' (prevents DVMHost FATAL PANIC error 4)")
                    modified = True

                raw_ch = sys_config_blk.get("channelNo", "1")
                hex_ch = format_channel_no(parse_channel_no(raw_ch))
                if not isinstance(raw_ch, str) or raw_ch != hex_ch:
                    sys_config_blk["channelNo"] = hex_ch
                    fixes_applied.append(f"Converted channelNo '{raw_ch}' to hexadecimal '${hex_ch}' for DVMHost")
                    modified = True

                root_rpc = cfg.get("network", {}).get("rpcPort", 9890)
                if "voiceChNo" in sys_config_blk and isinstance(sys_config_blk["voiceChNo"], list):
                    for vc in sys_config_blk["voiceChNo"]:
                        if isinstance(vc, dict):
                            vc_ch = vc.get("channelNo", hex_ch)
                            vc_hex = format_channel_no(parse_channel_no(vc_ch))
                            if not isinstance(vc_ch, str) or vc_ch != vc_hex:
                                vc["channelNo"] = vc_hex
                                modified = True
                            if vc.get("rpcPort") == root_rpc and root_rpc != 0:
                                vc["rpcPort"] = 0
                                fixes_applied.append("Set 'voiceChNo.rpcPort: 0' to eliminate RPC port conflict with main host daemon")
                                modified = True

            # Enforce logging settings for systemd journald
            if "log" not in cfg or not isinstance(cfg["log"], dict):
                cfg["log"] = {}
            if not cfg["log"].get("useSyslog"):
                cfg["log"]["useSyslog"] = True
                fixes_applied.append("Enabled 'log.useSyslog: true' (systemd journal output)")
                modified = True
            target_log_path = "/opt/dvm" if os.name != "nt" else str(self.dvm_dir)
            if cfg["log"].get("filePath") != target_log_path:
                cfg["log"]["filePath"] = target_log_path
                fixes_applied.append(f"Set 'log.filePath: {target_log_path}' absolute path")
                modified = True
            if cfg["log"].get("activityFilePath") != target_log_path:
                cfg["log"]["activityFilePath"] = target_log_path
                fixes_applied.append(f"Set 'log.activityFilePath: {target_log_path}' absolute path")
                modified = True

            if modified or bad_indent or not cfg_file.exists():
                dvm_config_manager.save_config_dict(cfg, summary="Auto-Fix: Corrected DVMHost policy, daemon, and YAML indentation")

            # 3. Restore any missing baseline data tables
            for tbl in ("iden_table.dat", "rid_acl.dat", "talkgroup_rules.yml"):
                target = self.dvm_dir / tbl
                if not target.exists() or target.stat().st_size == 0:
                    src_example = settings.profiles_dir / "default_dmr" / tbl
                    if not src_example.exists():
                        src_example = DEFAULT_CONFIGS_DIR / f"{Path(tbl).stem}.example{Path(tbl).suffix}"
                    if src_example.exists():
                        backup_manager.atomic_write(target, src_example.read_text(encoding="utf-8"))
                        fixes_applied.append(f"Restored missing data table '{tbl}' from template")

            # 3b. Sanitize iden_table.dat to fix invalid 0.0 MHz offsets
            target_iden = self.dvm_dir / "iden_table.dat"
            if target_iden.exists() and target_iden.stat().st_size > 0:
                try:
                    from dvm_nexus.config_engine.iden_calculator import read_iden_table_file, format_iden_table, IdenEntry
                    entries = read_iden_table_file(target_iden)
                    fixed_iden = False
                    for i, e in enumerate(entries):
                        if abs(e.offset_mhz) < 0.0001:
                            offset = 5.0 if 400000000 <= e.base_freq_hz <= 480000000 else 5.0
                            entries[i] = IdenEntry(
                                channel_id=e.channel_id,
                                base_freq_hz=e.base_freq_hz,
                                spacing_khz=e.spacing_khz,
                                offset_mhz=offset,
                                bandwidth_khz=e.bandwidth_khz
                            )
                            fixed_iden = True
                    if fixed_iden:
                        backup_manager.atomic_write(target_iden, format_iden_table(entries))
                        fixes_applied.append("Repaired invalid 0.0 MHz TX offset in iden_table.dat (updated to +5.0 MHz)")
                except Exception:
                    pass

            # 4. Fix permissions and systemd working directory on Linux so dvmhost can read and write files/logs
            if os.name != "nt":
                try:
                    subprocess.run(["chmod", "-R", "a+rwX", str(self.dvm_dir)], capture_output=True)
                    import pwd
                    try:
                        pwd.getpwnam("dvmhost")
                        subprocess.run(["chown", "-R", "dvmhost:dvmhost", str(self.dvm_dir)], capture_output=True)
                        subprocess.run(["usermod", "-a", "-G", "dialout,gpio", "dvmhost"], capture_output=True)
                    except KeyError:
                        pass

                    # Configure systemd drop-in override for dvmhost to ensure WorkingDirectory and write paths
                    override_dir = Path("/etc/systemd/system/dvmhost.service.d")
                    override_dir.mkdir(parents=True, exist_ok=True)
                    override_conf = override_dir / "override.conf"
                    override_conf.write_text(
                        "[Service]\n"
                        "WorkingDirectory=/opt/dvm\n"
                        "ReadWritePaths=/opt/dvm\n",
                        encoding="utf-8"
                    )

                    # Grant dialout permissions on serial devices and stop serial-getty on ttyAMA0
                    for dev in ["/dev/ttyAMA0", "/dev/serial0", "/dev/ttyUSB0", "/dev/ttyACM0"]:
                        if os.path.exists(dev):
                            subprocess.run(["chmod", "666", dev], capture_output=True)

                    subprocess.run(["systemctl", "stop", "serial-getty@ttyAMA0.service"], capture_output=True)
                    subprocess.run(["systemctl", "disable", "serial-getty@ttyAMA0.service"], capture_output=True)
                    subprocess.run(["systemctl", "mask", "serial-getty@ttyAMA0.service"], capture_output=True)
                    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
                    fixes_applied.append("Set permissions (chmod a+rwX, chown dvmhost), systemd WorkingDirectory=/opt/dvm, and UART access")

                    # Configure Raspberry Pi boot firmware for GPIO 14/15 UART
                    fw_dir = Path("/boot/firmware") if Path("/boot/firmware").is_dir() else Path("/boot")
                    cfg_txt = fw_dir / "config.txt"
                    cmdline_txt = fw_dir / "cmdline.txt"

                    if cfg_txt.exists():
                        b_content = cfg_txt.read_text(encoding="utf-8", errors="ignore")
                        orig_b = b_content
                        if "enable_uart=1" not in b_content:
                            b_content += "\nenable_uart=1\n"
                        if "dtparam=uart0=on" not in b_content:
                            b_content += "dtparam=uart0=on\n"
                        if "dtoverlay=uart0" not in b_content:
                            b_content += "dtoverlay=uart0\n"
                        if "dtoverlay=disable-bt" not in b_content:
                            b_content += "dtoverlay=disable-bt\n"
                        if b_content != orig_b:
                            cfg_txt.write_text(b_content, encoding="utf-8")
                            fixes_applied.append("Configured Pi 5 GPIO UART (dtparam=uart0=on, disable-bt) in config.txt")
                            reboot_needed = True

                    if cmdline_txt.exists():
                        cmd_content = cmdline_txt.read_text(encoding="utf-8", errors="ignore")
                        cleaned = re.sub(r'console=(serial0|ttyAMA0|tty1),[0-9]+\s*', '', cmd_content)
                        cleaned = re.sub(r'console=(serial0|ttyAMA0),[0-9]+', '', cleaned)
                        if cleaned != cmd_content:
                            cmdline_txt.write_text(cleaned, encoding="utf-8")
                            fixes_applied.append("Removed serial login console conflict from cmdline.txt")
                            reboot_needed = True
                except Exception:
                    pass

            # 5. Trigger restart of dvmhost service
            restart_res = self.service_controller.restart()
            time.sleep(1.5)
            new_status = self.service_controller.get_status()

            is_running = new_status.get("is_running", False)
            log_audit("DIAGNOSTICS", "AUTO_FIX", f"Auto-fix applied {len(fixes_applied)} remedies. Service running={is_running}")

            return {
                "success": is_running,
                "reboot_needed": reboot_needed,
                "fixes_applied": fixes_applied,
                "service_state": new_status.get("state"),
                "is_running": is_running,
                "message": (
                    f"Applied {len(fixes_applied)} automatic fixes. DVMHost is now RUNNING!"
                    if is_running else
                    (f"Applied {len(fixes_applied)} hardware/config fixes. A system reboot is required for Raspberry Pi 5 GPIO UART to take effect."
                     if reboot_needed else
                     f"Applied {len(fixes_applied)} fixes, but service did not stay running ({restart_res.get('message')}). Check crash logs below.")
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
