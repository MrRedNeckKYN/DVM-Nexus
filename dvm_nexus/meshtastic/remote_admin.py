"""
DVM Nexus - Secure Remote Management Processor for Meshtastic
Implements administrator authentication, authorization, rate limiting,
two-step confirmation tokens for disruptive actions, and audit logging.
"""

import time
import secrets
import string
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple
from dvm_nexus.core.database import get_db_connection, log_audit
from dvm_nexus.core.system_monitor import get_system_metrics
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.profile_manager import profile_manager

logger = logging.getLogger("dvm_nexus.meshtastic.remote_admin")


class RemoteAdminProcessor:
    """Processes authenticated management instructions received over Meshtastic."""

    def __init__(self):
        # In-memory rate limiting: {node_id: [timestamps]}
        self._rate_limits: Dict[str, list] = {}
        self.max_commands_per_min = 5

    def is_admin_authorized(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Check if node_id is an authorized administrator."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT node_id, callsign_or_name, permission_level, can_restart_service, can_switch_profile
                FROM meshtastic_admins WHERE node_id = ?
            """, (node_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def check_rate_limit(self, node_id: str) -> bool:
        """Enforce max commands per minute per node."""
        now = time.time()
        timestamps = self._rate_limits.setdefault(node_id, [])
        # Prune older than 60s
        self._rate_limits[node_id] = [t for t in timestamps if now - t < 60.0]
        if len(self._rate_limits[node_id]) >= self.max_commands_per_min:
            return False
        self._rate_limits[node_id].append(now)
        return True

    def process_command(self, sender_id: str, raw_text: str) -> Optional[str]:
        """
        Evaluate and execute incoming admin command if authorized.
        Returns a response string to be transmitted back, or None if ignored.
        """
        cmd_text = raw_text.strip()
        if not cmd_text.startswith("!"):
            return None  # Regular text message, not an administrative command

        admin = self.is_admin_authorized(sender_id)
        if not admin:
            log_audit("MESHTASTIC_ADMIN", "UNAUTHORIZED_ATTEMPT",
                      f"Unauthorized node {sender_id} attempted command: {cmd_text[:50]}", status="rejected")
            return f"[Nexus] Access Denied: Node {sender_id} is not an authorized administrator."

        if not self.check_rate_limit(sender_id):
            log_audit("MESHTASTIC_ADMIN", "RATE_LIMIT",
                      f"Node {sender_id} exceeded command rate limit", status="warning")
            return "[Nexus] Rate limit exceeded (max 5 commands/minute). Please wait."

        parts = cmd_text[1:].split()
        if not parts:
            return None

        verb = parts[0].lower()
        args = parts[1:]

        # Execute safe, read-only commands
        if verb == "ping":
            return "[Nexus] Pong! DVM Nexus is operational."

        elif verb == "help":
            return (
                "[Nexus Commands]\n"
                "!status - System & Radio Status\n"
                "!modem - Modem RF Diagnostics\n"
                "!profiles - List saved profiles\n"
                "!profile <name> - Switch radio profile (requires confirm)\n"
                "!restart - Restart DVMHost (requires confirm)\n"
                "!confirm <token> - Confirm pending action"
            )

        elif verb == "status":
            return self._cmd_status()

        elif verb == "modem":
            return self._cmd_modem()

        elif verb == "profiles":
            return self._cmd_profiles()

        elif verb == "restart":
            return self._cmd_request_restart(sender_id, admin)

        elif verb == "profile" and args:
            target_profile = " ".join(args)
            return self._cmd_request_profile_switch(sender_id, admin, target_profile)

        elif verb == "confirm" and args:
            token = args[0].strip()
            return self._cmd_confirm(sender_id, token)

        else:
            return f"[Nexus] Unknown command '{verb}'. Send '!help' for command list."

    def _cmd_status(self) -> str:
        telemetry = get_system_metrics()
        svc_status = service_controller.get_status()
        active_prof = profile_manager.get_active_profile()
        prof_name = active_prof.get("name") if active_prof else "Unknown"
        protocol = active_prof.get("protocol", "unknown").upper() if active_prof else "N/A"

        temp_str = telemetry.get("cpu_temp_display") or "N/A"
        return (
            f"[Nexus Status]\n"
            f"Radio: {prof_name} ({protocol})\n"
            f"DVMHost: {svc_status.get('state', 'unknown').upper()}\n"
            f"Pi 5 Temp: {temp_str}\n"
            f"CPU: {telemetry.get('cpu_percent', 0)}% | RAM: {telemetry.get('memory_percent', 0)}%\n"
            f"Uptime: {telemetry.get('uptime', 'N/A')}"
        )

    def _cmd_modem(self) -> str:
        svc_status = service_controller.get_status()
        active = svc_status.get("is_running", False)
        return (
            f"[Modem Diagnostics]\n"
            f"Device: /dev/ttyAMA0 (UART 115200)\n"
            f"Hardware: MMDVM Duplex (Dual ADF7021)\n"
            f"Carrier RF: {'ACTIVE (TX/RX)' if active else 'STANDBY'}\n"
            f"TCXO: 14.7456 MHz"
        )

    def _cmd_profiles(self) -> str:
        profs = profile_manager.list_profiles()
        lines = ["[Saved Profiles]"]
        for p in profs:
            mark = "* " if p.get("is_active") else "  "
            lines.append(f"{mark}{p['name']} ({p['protocol'].upper()})")
        return "\n".join(lines)

    def _cmd_request_restart(self, sender_id: str, admin: Dict[str, Any]) -> str:
        if not admin.get("can_restart_service"):
            log_audit("MESHTASTIC_ADMIN", "PERMISSION_DENIED",
                      f"Node {sender_id} lacked restart permission", status="rejected")
            return "[Nexus] Permission denied: You do not have service restart privileges."

        token = self._generate_token(sender_id, "restart_service")
        log_audit("MESHTASTIC_ADMIN", "RESTART_REQUESTED",
                  f"Restart token {token} generated for {admin.get('callsign_or_name')} ({sender_id})")
        return (
            f"[Nexus Security]\n"
            f"DVMHost restart requested.\n"
            f"Send '!confirm {token}' within 60s to execute."
        )

    def _cmd_request_profile_switch(self, sender_id: str, admin: Dict[str, Any], target_name: str) -> str:
        if not admin.get("can_switch_profile"):
            log_audit("MESHTASTIC_ADMIN", "PERMISSION_DENIED",
                      f"Node {sender_id} lacked profile switch permission", status="rejected")
            return "[Nexus] Permission denied: You do not have radio profile switch privileges."

        profs = profile_manager.list_profiles()
        match = next((p for p in profs if p["name"].lower() == target_name.lower() or p["id"] == target_name), None)
        if not match:
            return f"[Nexus] Profile '{target_name}' not found. Send '!profiles' to view list."

        if match.get("is_active"):
            return f"[Nexus] Profile '{match['name']}' is already active."

        token = self._generate_token(sender_id, f"switch_profile:{match['id']}")
        log_audit("MESHTASTIC_ADMIN", "PROFILE_SWITCH_REQUESTED",
                  f"Switch to '{match['name']}' requested by {admin.get('callsign_or_name')} (Token: {token})")
        return (
            f"[Nexus Security]\n"
            f"Switch to profile '{match['name']}' ({match['protocol'].upper()}) requested.\n"
            f"Send '!confirm {token}' within 60s to apply."
        )

    def _generate_token(self, node_id: str, action: str) -> str:
        token = "".join(secrets.choice(string.digits) for _ in range(4))
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()

        conn = get_db_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO meshtastic_tokens (token, node_id, action, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (token, node_id, action, expires_at, now_str))
            return token
        finally:
            conn.close()

    def _cmd_confirm(self, sender_id: str, token: str) -> str:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT token, node_id, action, expires_at FROM meshtastic_tokens
                WHERE token = ? AND node_id = ?
            """, (token, sender_id))
            row = cur.fetchone()

            if not row:
                return "[Nexus] Invalid or expired confirmation token."

            row_dict = dict(row)
            expires_at = datetime.fromisoformat(row_dict["expires_at"])
            if datetime.now(timezone.utc) > expires_at:
                conn.execute("DELETE FROM meshtastic_tokens WHERE token = ?", (token,))
                conn.commit()
                return "[Nexus] Confirmation token has expired. Please re-issue the command."

            # Delete token so it cannot be re-used
            conn.execute("DELETE FROM meshtastic_tokens WHERE token = ?", (token,))
            conn.commit()

            action = row_dict["action"]
            if action == "restart_service":
                log_audit("MESHTASTIC_ADMIN", "RESTART_EXECUTED", f"Service restart confirmed by {sender_id}")
                res = service_controller.restart()
                if res.get("success"):
                    return "[Nexus SUCCESS] DVMHost service successfully restarted."
                else:
                    return f"[Nexus ERROR] Service restart failed: {res.get('message')}"

            elif action.startswith("switch_profile:"):
                target_id = action.split(":", 1)[1]
                prof = profile_manager.get_profile(target_id)
                prof_name = prof.get("name", target_id) if prof else target_id

                log_audit("MESHTASTIC_ADMIN", "PROFILE_SWITCH_EXECUTED",
                          f"Profile switch to '{prof_name}' confirmed by {sender_id}")
                res = profile_manager.activate_profile(target_id)
                if res.get("success"):
                    return f"[Nexus SUCCESS] Profile '{prof_name}' activated and DVMHost restarted."
                else:
                    return f"[Nexus ERROR] Profile switch failed: {res.get('error')}"

            return "[Nexus] Unknown confirmed action."
        finally:
            conn.close()


remote_admin_processor = RemoteAdminProcessor()
