"""
DVM Nexus - Bidirectional Radio <-> Meshtastic Text Messaging Gateway
Enables two-way text messaging (SMS/TMS/PDU) between DMR/P25 radio networks and Meshtastic nodes.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from dvm_nexus.core.database import get_db_connection, log_audit
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.config_engine.profile_manager import ProfileManager

logger = logging.getLogger("dvm_nexus.meshtastic.text_gateway")


class RadioTextGateway:
    """Manages bidirectional text message translation and routing between digital radio and Meshtastic."""

    def __init__(self, meshtastic_manager=None):
        self._manager = meshtastic_manager

    def set_manager(self, manager):
        self._manager = manager

    def get_config(self) -> Dict[str, str]:
        """Load text bridge configuration."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM text_bridge_config")
            return dict(cur.fetchall())
        finally:
            conn.close()

    def save_config(self, **kwargs):
        """Update text bridge configuration."""
        conn = get_db_connection()
        try:
            with conn:
                for k, v in kwargs.items():
                    conn.execute("INSERT OR REPLACE INTO text_bridge_config (key, value) VALUES (?, ?)", (k, str(v)))
        finally:
            conn.close()

    def get_recent_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent bridged text messages."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, protocol, direction, radio_src_id, radio_dest_id, is_group,
                       mesh_node_id, mesh_channel, text, status
                FROM radio_text_messages ORDER BY id DESC LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def handle_meshtastic_inbound_text(self, sender_id: str, sender_name: str, text: str, channel: int) -> Optional[Dict[str, Any]]:
        """
        Called when a text message arrives from Meshtastic.
        Determines if it should be bridged to DMR or P25, formats it, and transmits over radio.
        """
        cfg = self.get_config()
        if cfg.get("enabled", "1") != "1":
            return None

        # Ignore administrative commands
        if text.startswith("!"):
            return None

        clean_text = text.strip()
        if not clean_text:
            return None

        target_protocol = None
        target_id = None
        is_group = True
        body_text = clean_text

        # 1. Check for prefix routing: e.g. #dmr 3100 Hello or @p25 101 Test
        prefix_match = re.match(r"^[@#](dmr|p25)(?:\s+(\d+))?\s+(.+)$", clean_text, re.IGNORECASE)
        if prefix_match:
            target_protocol = prefix_match.group(1).lower()
            if prefix_match.group(2):
                target_id = int(prefix_match.group(2))
            body_text = prefix_match.group(3).strip()

        # 2. Check channel-based mapping
        if not target_protocol:
            dmr_ch = int(cfg.get("dmr_channel", "1"))
            p25_ch = int(cfg.get("p25_channel", "2"))

            if channel == dmr_ch:
                target_protocol = "dmr"
                target_id = int(cfg.get("default_dmr_tg", "3100"))
            elif channel == p25_ch:
                target_protocol = "p25"
                target_id = int(cfg.get("default_p25_tg", "101"))

        # 3. Fallback: forward all primary channel traffic to the active radio protocol
        if not target_protocol and cfg.get("forward_all_to_active", "1") == "1":
            pm = ProfileManager()
            active_prof = pm.get_active_profile()
            if active_prof:
                target_protocol = active_prof.get("protocol", "dmr")
                default_tg = cfg.get(f"default_{target_protocol}_tg", "3100" if target_protocol == "dmr" else "101")
                target_id = int(default_tg)

        if not target_protocol or not target_id:
            return None  # No routing rule matched

        # Format caller header for radio screen: "[Mesh:N3XYZ]: Hello world"
        radio_payload = f"[{sender_name or sender_id}]: {body_text}"
        # Keep within standard radio TMS length limits (typically 140 chars)
        if len(radio_payload) > 140:
            radio_payload = radio_payload[:137] + "..."

        # Transmit over the air via DVMHost REST API
        if target_protocol == "dmr":
            res = dvm_client.dmr_send_text(text=radio_payload, tg_or_rid=target_id, is_group=is_group)
        else:
            res = dvm_client.p25_send_text(text=radio_payload, tg_or_rid=target_id, is_group=is_group)

        # Log into radio_text_messages database
        now_str = datetime.now(timezone.utc).isoformat()
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO radio_text_messages (timestamp, protocol, direction, radio_src_id, radio_dest_id, is_group, mesh_node_id, mesh_channel, text, status)
                    VALUES (?, ?, 'mesh_to_radio', 0, ?, ?, ?, ?, ?, 'delivered')
                """, (now_str, target_protocol, target_id, 1 if is_group else 0, sender_id, channel, body_text))
        finally:
            conn.close()

        log_audit("TEXT_BRIDGE", "MESH_TO_RADIO",
                  f"Bridged Mesh '{sender_name}' ({sender_id}) -> {target_protocol.upper()} TG {target_id}: '{body_text}'")

        return {
            "success": True,
            "protocol": target_protocol,
            "target_id": target_id,
            "text": body_text
        }

    def handle_radio_inbound_text(self, protocol: str, src_id: int, dest_id: int, is_group: bool, text: str) -> Dict[str, Any]:
        """
        Called when a text message is received from a DMR or P25 radio over the air.
        Formats and transmits the text into the Meshtastic mesh.
        """
        cfg = self.get_config()
        if cfg.get("enabled", "1") != "1":
            return {"success": False, "error": "Text bridge is disabled"}

        protocol = protocol.lower()
        clean_text = text.strip()
        if not clean_text:
            return {"success": False, "error": "Empty message text"}

        # Target mesh channel (use dedicated protocol channel or channel 0)
        dest_channel = int(cfg.get(f"{protocol}_channel", "1" if protocol == "dmr" else "2"))

        # Format message for mesh display
        tag = f"[{protocol.upper()} TG {dest_id} from RID {src_id}]" if is_group else f"[{protocol.upper()} Private from RID {src_id}]"
        mesh_payload = f"{tag}: {clean_text}"

        # Send over Meshtastic
        if self._manager and self._manager.is_connected():
            self._manager.send_message(
                text=mesh_payload,
                recipient="^all",
                channel=dest_channel,
                is_system=True
            )

        # Record in database
        now_str = datetime.now(timezone.utc).isoformat()
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO radio_text_messages (timestamp, protocol, direction, radio_src_id, radio_dest_id, is_group, mesh_node_id, mesh_channel, text, status)
                    VALUES (?, ?, 'radio_to_mesh', ?, ?, ?, '^all', ?, ?, 'delivered')
                """, (now_str, protocol, src_id, dest_id, 1 if is_group else 0, dest_channel, clean_text))
        finally:
            conn.close()

        log_audit("TEXT_BRIDGE", "RADIO_TO_MESH",
                  f"Bridged {protocol.upper()} (RID {src_id} -> TG {dest_id}) -> Mesh Ch {dest_channel}: '{clean_text}'")

        return {"success": True, "mesh_channel": dest_channel, "text": mesh_payload}

    def send_text_from_web(self, protocol: str, dest_id: int, is_group: bool, text: str, forward_to_mesh: bool = True) -> Dict[str, Any]:
        """
        Send a text message from the WebUI out to the radio network, optionally forwarding to Meshtastic.
        """
        protocol = protocol.lower()
        clean_text = text.strip()
        if not clean_text:
            return {"success": False, "error": "Text cannot be empty"}

        # 1. Transmit over radio
        if protocol == "dmr":
            dvm_client.dmr_send_text(text=clean_text, tg_or_rid=dest_id, is_group=is_group)
        else:
            dvm_client.p25_send_text(text=clean_text, tg_or_rid=dest_id, is_group=is_group)

        # 2. Forward to mesh if requested
        cfg = self.get_config()
        dest_channel = int(cfg.get(f"{protocol}_channel", "0"))
        if forward_to_mesh and self._manager and self._manager.is_connected():
            mesh_text = f"[{protocol.upper()} TG {dest_id}]: {clean_text}"
            self._manager.send_message(text=mesh_text, recipient="^all", channel=dest_channel, is_system=True)

        # 3. Log
        now_str = datetime.now(timezone.utc).isoformat()
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO radio_text_messages (timestamp, protocol, direction, radio_src_id, radio_dest_id, is_group, mesh_node_id, mesh_channel, text, status)
                    VALUES (?, ?, 'web_to_radio', 0, ?, ?, '^all', ?, ?, 'delivered')
                """, (now_str, protocol, dest_id, 1 if is_group else 0, dest_channel, clean_text))
        finally:
            conn.close()

        log_audit("TEXT_BRIDGE", "WEB_SEND", f"Sent {protocol.upper()} text to {dest_id}: '{clean_text}'")
        return {"success": True, "protocol": protocol, "dest_id": dest_id, "text": clean_text}


text_gateway = RadioTextGateway()
