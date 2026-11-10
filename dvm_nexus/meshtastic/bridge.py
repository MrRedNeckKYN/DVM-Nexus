"""
DVM Nexus - DMR / P25 to Meshtastic Event Bridge
Routes radio network events and system status notifications to Meshtastic channels,
and triggers actions on incoming mesh activity according to configured rules.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from dvm_nexus.core.database import get_db_connection, log_audit

logger = logging.getLogger("dvm_nexus.meshtastic.bridge")


class MeshtasticBridge:
    """Manages event routing between DVM radio networks and Meshtastic."""

    def __init__(self, meshtastic_manager=None):
        self._manager = meshtastic_manager

    def set_manager(self, manager):
        self._manager = manager

    def get_routes(self) -> List[Dict[str, Any]]:
        """Return all configured event routing rules."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, source_type, source_id, dest_channel, dest_node, template, enabled, created_at
                FROM meshtastic_routes ORDER BY id ASC
            """)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def add_route(self, name: str, source_type: str, source_id: str,
                  dest_channel: int = 0, dest_node: str = "^all", template: str = "{event_text}") -> int:
        """Create a new routing rule."""
        conn = get_db_connection()
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO meshtastic_routes (name, source_type, source_id, dest_channel, dest_node, template, enabled, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """, (name, source_type, str(source_id), dest_channel, dest_node, template, now_str))
                route_id = cur.lastrowid
            log_audit("MESHTASTIC_BRIDGE", "ADD_ROUTE", f"Added route '{name}' for {source_type}:{source_id}")
            return route_id
        finally:
            conn.close()

    def delete_route(self, route_id: int) -> bool:
        """Delete an existing routing rule."""
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM meshtastic_routes WHERE id = ?", (route_id,))
            log_audit("MESHTASTIC_BRIDGE", "DELETE_ROUTE", f"Deleted route ID {route_id}")
            return True
        finally:
            conn.close()

    def toggle_route(self, route_id: int, enabled: bool) -> bool:
        """Enable or disable a routing rule."""
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("UPDATE meshtastic_routes SET enabled = ? WHERE id = ?", (1 if enabled else 0, route_id))
            return True
        finally:
            conn.close()

    def handle_radio_event(self, source_type: str, source_id: str, event_text: str, metadata: Optional[Dict[str, Any]] = None):
        """
        Evaluate an event from DMR, P25, or System Status and dispatch matching notifications.
        source_type: 'dmr_talkgroup', 'p25_talkgroup', or 'system_status'
        source_id: e.g. '3100', '101', 'dvmhost'
        """
        if not self._manager or not self._manager.is_connected():
            return

        routes = self.get_routes()
        for r in routes:
            if not r.get("enabled"):
                continue

            # Check source type match
            if r["source_type"] != source_type:
                continue

            # Check source ID match ('all', '*', or exact ID)
            rule_src = str(r["source_id"]).strip().lower()
            evt_src = str(source_id).strip().lower()
            if rule_src not in ("all", "*", evt_src):
                continue

            # Format message
            template = r.get("template", "{event_text}")
            msg = template.format(
                event_text=event_text,
                source_id=source_id,
                source_type=source_type,
                timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S")
            )

            # Send via Meshtastic
            dest_channel = r.get("dest_channel", 0)
            dest_node = r.get("dest_node", "^all")
            logger.info("Bridge routing event -> Meshtastic: '%s' to %s ch=%s", msg, dest_node, dest_channel)
            self._manager.send_message(text=msg, recipient=dest_node, channel=dest_channel, is_system=True)


meshtastic_bridge = MeshtasticBridge()
