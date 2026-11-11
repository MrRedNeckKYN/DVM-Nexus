"""
DVM Nexus - Central Meshtastic Manager
Coordinates transport connections, message logging, real-time message handling,
and dispatches to the secure remote administrator processor.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from dvm_nexus.core.database import get_db_connection, log_audit
from dvm_nexus.meshtastic.transport import (
    BaseMeshtasticTransport, SerialTransport, TCPTransport, BLETransport, MockTransport
)
from dvm_nexus.meshtastic.remote_admin import remote_admin_processor
from dvm_nexus.meshtastic.bridge import meshtastic_bridge
from dvm_nexus.meshtastic.text_gateway import text_gateway
from dvm_nexus.meshtastic.voice_gateway import voice_gateway

logger = logging.getLogger("dvm_nexus.meshtastic.client")


class MeshtasticManager:
    """Singleton coordinator for Meshtastic mesh operations."""

    def __init__(self):
        self.transport: Optional[BaseMeshtasticTransport] = None
        self.transport_type: str = "mock"  # default to mock until configured
        self.last_error: Optional[str] = None
        self._load_config()

        # Connect the bridge and text gateway
        meshtastic_bridge.set_manager(self)
        text_gateway.set_manager(self)
        voice_gateway.set_manager(self)
        # Start voice gateway UDP listener
        voice_gateway.start()

        # Initialize default transport
        self._init_transport()

    def _load_config(self):
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM meshtastic_config")
            rows = dict(cur.fetchall())
            self.transport_type = rows.get("transport_type", "mock")
            self.serial_port = rows.get("serial_port", "auto")
            self.tcp_host = rows.get("tcp_host", "127.0.0.1")
            self.tcp_port = int(rows.get("tcp_port", "4403"))
            self.ble_address = rows.get("ble_address", "")
            self.auto_connect = rows.get("auto_connect", "1") == "1"
        except Exception as e:
            logger.warning("Could not load meshtastic_config from DB: %s", e)
            self.transport_type = "mock"
            self.serial_port = "auto"
            self.tcp_host = "127.0.0.1"
            self.tcp_port = 4403
            self.ble_address = ""
            self.auto_connect = True
        finally:
            conn.close()

    def save_config(self, transport_type: str, serial_port: str = "auto",
                    tcp_host: str = "127.0.0.1", tcp_port: int = 4403,
                    ble_address: str = "", auto_connect: bool = True):
        self.transport_type = transport_type
        self.serial_port = serial_port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.ble_address = ble_address
        self.auto_connect = auto_connect

        conn = get_db_connection()
        try:
            with conn:
                for k, v in [
                    ("transport_type", transport_type),
                    ("serial_port", serial_port),
                    ("tcp_host", tcp_host),
                    ("tcp_port", str(tcp_port)),
                    ("ble_address", ble_address),
                    ("auto_connect", "1" if auto_connect else "0")
                ]:
                    conn.execute("INSERT OR REPLACE INTO meshtastic_config (key, value) VALUES (?, ?)", (k, v))
        finally:
            conn.close()

    def _init_transport(self):
        """Create the transport instance and register callbacks."""
        if self.transport:
            try:
                self.transport.disconnect()
            except Exception:
                pass

        if self.transport_type == "serial":
            self.transport = SerialTransport(port=self.serial_port)
        elif self.transport_type == "tcp":
            self.transport = TCPTransport(hostname=self.tcp_host, port=self.tcp_port)
        elif self.transport_type == "ble":
            self.transport = BLETransport(address=self.ble_address)
        else:
            self.transport = MockTransport()

        self.transport.set_callbacks(
            on_message=self._handle_inbound_packet,
            on_node=self._handle_node_update
        )

        if self.auto_connect:
            self.connect()

    def connect(self) -> Dict[str, Any]:
        """Establish mesh connection."""
        if not self.transport:
            self._init_transport()

        self.last_error = None
        ok = self.transport.connect()
        if ok:
            log_audit("MESHTASTIC", "CONNECT", f"Connected via {self.transport_type.upper()}")
            return {"success": True, "transport": self.transport_type}
        else:
            self.last_error = f"Failed to connect using {self.transport_type}"
            log_audit("MESHTASTIC", "CONNECT_FAILED", self.last_error, status="error")
            return {"success": False, "error": self.last_error}

    def disconnect(self) -> Dict[str, Any]:
        """Disconnect active transport."""
        if self.transport:
            self.transport.disconnect()
            log_audit("MESHTASTIC", "DISCONNECT", "Meshtastic disconnected")
        return {"success": True}

    def is_connected(self) -> bool:
        return self.transport is not None and self.transport.is_connected()

    def get_status(self) -> Dict[str, Any]:
        """Return comprehensive status overview."""
        info = self.transport.get_my_node_info() if self.transport else {}
        nodes = self.transport.get_nodes() if self.transport else []
        return {
            "connected": self.is_connected(),
            "transport_type": self.transport_type,
            "serial_port": self.serial_port,
            "tcp_host": f"{self.tcp_host}:{self.tcp_port}",
            "ble_address": self.ble_address,
            "node_count": len(nodes),
            "my_info": info,
            "last_error": self.last_error
        }

    def get_nodes(self) -> List[Dict[str, Any]]:
        """Return list of known nodes."""
        if not self.transport:
            return []
        return self.transport.get_nodes()

    def send_message(self, text: str, recipient: str = "^all", channel: int = 0, is_system: bool = False) -> Dict[str, Any]:
        """Send a text message and persist to database."""
        if not self.is_connected():
            return {"success": False, "error": "Meshtastic is not connected"}

        text = text.strip()
        if not text:
            return {"success": False, "error": "Message text cannot be empty"}

        ok = self.transport.send_text(text=text, destination=recipient, channel_index=channel)
        if not ok:
            return {"success": False, "error": "Transport failed to deliver message"}

        # Persist to database
        conn = get_db_connection()
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO meshtastic_messages (timestamp, direction, sender_id, sender_name, recipient_id, channel, text, acknowledged)
                    VALUES (?, 'outbound', 'Nexus Base', 'DVM Nexus', ?, ?, ?, 1)
                """, (now_str, recipient, channel, text))
        finally:
            conn.close()

        if not is_system:
            log_audit("MESHTASTIC_MSG", "SEND", f"Sent message to {recipient} (ch {channel}): {text[:40]}")

        return {"success": True, "timestamp": now_str}

    def get_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent message history."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, direction, sender_id, sender_name, recipient_id, channel, text, acknowledged, snr, rssi
                FROM meshtastic_messages ORDER BY id DESC LIMIT ?
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            # Return in chronological order
            return list(reversed(rows))
        finally:
            conn.close()

    def _handle_inbound_packet(self, packet: Dict[str, Any]):
        """Callback executed when an incoming message is received."""
        sender_id = packet.get("sender", "unknown")
        text = packet.get("text", "")
        channel = packet.get("channel", 0)
        recipient = packet.get("recipient", "^all")
        snr = packet.get("snr")
        rssi = packet.get("rssi")

        # Resolve sender name from nodes directory
        sender_name = "Unknown Node"
        if self.transport:
            for n in self.transport.get_nodes():
                if n.get("id") == sender_id:
                    sender_name = n.get("long_name", sender_id)
                    break

        # Persist inbound message
        conn = get_db_connection()
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO meshtastic_messages (timestamp, direction, sender_id, sender_name, recipient_id, channel, text, acknowledged, snr, rssi)
                    VALUES (?, 'inbound', ?, ?, ?, ?, ?, 1, ?, ?)
                """, (now_str, sender_id, sender_name, recipient, channel, text, snr, rssi))
        finally:
            conn.close()

        logger.info("Meshtastic Inbound RX from %s (%s): '%s'", sender_name, sender_id, text)

        # Check for remote administration command (starts with '!')
        if text.startswith("!"):
            reply = remote_admin_processor.process_command(sender_id, text)
            if reply:
                # Send response back to the requesting administrator
                self.send_message(text=reply, recipient=sender_id, channel=channel, is_system=True)
        else:
            # Bidirectional Radio Text Messaging Gateway: Route incoming mesh text to DMR / P25
            try:
                text_gateway.handle_meshtastic_inbound_text(
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                    channel=channel
                )
            except Exception as e:
                logger.error("Error in text gateway bridge: %s", e)

            # Bidirectional Voice TTS Bridge: Synthesize speech and stream to dvmbridge
            try:
                voice_gateway.handle_meshtastic_inbound_text(
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                    channel=channel
                )
            except Exception as e:
                logger.error("Error in voice gateway TTS bridge: %s", e)

    def _handle_node_update(self, node_data: Dict[str, Any]):
        pass


meshtastic_manager = MeshtasticManager()
