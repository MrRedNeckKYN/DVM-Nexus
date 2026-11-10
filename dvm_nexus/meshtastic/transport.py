"""
DVM Nexus - Meshtastic Transport Abstraction Layer
Supports USB Serial, TCP/WiFi, BLE, and Simulated (Mock) transports.
"""

import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger("dvm_nexus.meshtastic.transport")


class BaseMeshtasticTransport(ABC):
    """Abstract base transport for Meshtastic hardware connections."""

    def __init__(self):
        self._on_message_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_node_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_callbacks(self, on_message: Callable[[Dict[str, Any]], None], on_node: Callable[[Dict[str, Any]], None]):
        self._on_message_callback = on_message
        self._on_node_callback = on_node

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to Meshtastic device."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to Meshtastic device."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if connection is alive."""
        pass

    @abstractmethod
    def send_text(self, text: str, destination: str = "^all", channel_index: int = 0) -> bool:
        """Send a text message over the mesh."""
        pass

    @abstractmethod
    def get_nodes(self) -> List[Dict[str, Any]]:
        """Return list of known mesh nodes."""
        pass

    @abstractmethod
    def get_my_node_info(self) -> Dict[str, Any]:
        """Return local device node info."""
        pass


class SerialTransport(BaseMeshtasticTransport):
    """USB Serial transport using official meshtastic.serial_interface."""

    def __init__(self, port: Optional[str] = None):
        super().__init__()
        self.port = port
        self.interface = None

    def connect(self) -> bool:
        try:
            import meshtastic.serial_interface
            from pubsub import pub

            target_port = self.port
            if not target_port or target_port == "auto":
                target_port = None  # None causes meshtastic to auto-detect

            logger.info("Connecting to Meshtastic via Serial port: %s", target_port or "auto-detect")
            self.interface = meshtastic.serial_interface.SerialInterface(devPath=target_port)

            pub.subscribe(self._handle_rx, "meshtastic.receive")
            return True
        except Exception as e:
            logger.error("Failed to connect Meshtastic Serial: %s", e)
            self.interface = None
            return False

    def disconnect(self) -> None:
        if self.interface:
            try:
                from pubsub import pub
                pub.unsubscribe(self._handle_rx, "meshtastic.receive")
            except Exception:
                pass
            try:
                self.interface.close()
            except Exception as e:
                logger.warning("Error closing SerialInterface: %s", e)
            self.interface = None

    def is_connected(self) -> bool:
        return self.interface is not None and getattr(self.interface, "isConnected", True)

    def send_text(self, text: str, destination: str = "^all", channel_index: int = 0) -> bool:
        if not self.is_connected():
            return False
        try:
            dest_id = destination if destination != "^all" else None
            self.interface.sendText(text, destinationId=dest_id, channelIndex=channel_index)
            return True
        except Exception as e:
            logger.error("Failed to send text over Meshtastic serial: %s", e)
            return False

    def get_nodes(self) -> List[Dict[str, Any]]:
        if not self.is_connected():
            return []
        nodes = []
        raw_nodes = getattr(self.interface, "nodes", {}) or {}
        for num, data in raw_nodes.items():
            user = data.get("user", {})
            metrics = data.get("deviceMetrics", {})
            pos = data.get("position", {})
            nodes.append({
                "num": num,
                "id": user.get("id", f"!{num:08x}"),
                "long_name": user.get("longName", "Unknown Node"),
                "short_name": user.get("shortName", "????"),
                "hw_model": user.get("hwModel", "Unknown"),
                "battery": metrics.get("batteryLevel", None),
                "voltage": metrics.get("voltage", None),
                "snr": data.get("snr", None),
                "latitude": pos.get("latitude", None),
                "longitude": pos.get("longitude", None),
                "altitude": pos.get("altitude", None),
                "last_heard": data.get("lastHeard", None)
            })
        return nodes

    def get_my_node_info(self) -> Dict[str, Any]:
        if not self.is_connected():
            return {"connected": False}
        my_info = getattr(self.interface, "myInfo", None)
        my_node_num = getattr(my_info, "my_node_num", 0) if my_info else 0
        nodes = self.get_nodes()
        local_node = next((n for n in nodes if n["num"] == my_node_num), None)
        return {
            "connected": True,
            "transport": "serial",
            "port": self.port or "auto",
            "my_node_num": my_node_num,
            "node": local_node
        }

    def _handle_rx(self, packet: Any, interface: Any = None):
        if not self._on_message_callback:
            return
        try:
            decoded = packet.get("decoded", {})
            portnum = decoded.get("portnum")
            if portnum in ("TEXT_MESSAGE_APP", 1) or "text" in decoded:
                sender = packet.get("fromId", f"!{packet.get('from', 0):08x}")
                to_id = packet.get("toId", "^all")
                channel = packet.get("channel", 0)
                text = decoded.get("text", "")
                rx_snr = packet.get("rxSnr")
                rx_rssi = packet.get("rxRssi")
                self._on_message_callback({
                    "sender": sender,
                    "recipient": to_id,
                    "channel": channel,
                    "text": text,
                    "snr": rx_snr,
                    "rssi": rx_rssi,
                    "timestamp": time.time()
                })
        except Exception as e:
            logger.error("Error processing Meshtastic RX packet: %s", e)


class TCPTransport(BaseMeshtasticTransport):
    """TCP transport using official meshtastic.tcp_interface."""

    def __init__(self, hostname: str = "127.0.0.1", port: int = 4403):
        super().__init__()
        self.hostname = hostname
        self.port = port
        self.interface = None

    def connect(self) -> bool:
        try:
            import meshtastic.tcp_interface
            from pubsub import pub

            logger.info("Connecting to Meshtastic via TCP: %s:%s", self.hostname, self.port)
            self.interface = meshtastic.tcp_interface.TCPInterface(
                hostname=self.hostname,
                portNumber=self.port
            )
            pub.subscribe(self._handle_rx, "meshtastic.receive")
            return True
        except Exception as e:
            logger.error("Failed to connect Meshtastic TCP: %s", e)
            self.interface = None
            return False

    def disconnect(self) -> None:
        if self.interface:
            try:
                from pubsub import pub
                pub.unsubscribe(self._handle_rx, "meshtastic.receive")
            except Exception:
                pass
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None

    def is_connected(self) -> bool:
        return self.interface is not None and getattr(self.interface, "isConnected", True)

    def send_text(self, text: str, destination: str = "^all", channel_index: int = 0) -> bool:
        if not self.is_connected():
            return False
        try:
            dest_id = destination if destination != "^all" else None
            self.interface.sendText(text, destinationId=dest_id, channelIndex=channel_index)
            return True
        except Exception as e:
            logger.error("Failed to send text over Meshtastic TCP: %s", e)
            return False

    def get_nodes(self) -> List[Dict[str, Any]]:
        if not self.is_connected():
            return []
        nodes = []
        raw_nodes = getattr(self.interface, "nodes", {}) or {}
        for num, data in raw_nodes.items():
            user = data.get("user", {})
            metrics = data.get("deviceMetrics", {})
            pos = data.get("position", {})
            nodes.append({
                "num": num,
                "id": user.get("id", f"!{num:08x}"),
                "long_name": user.get("longName", "Unknown Node"),
                "short_name": user.get("shortName", "????"),
                "hw_model": user.get("hwModel", "Unknown"),
                "battery": metrics.get("batteryLevel", None),
                "voltage": metrics.get("voltage", None),
                "snr": data.get("snr", None),
                "latitude": pos.get("latitude", None),
                "longitude": pos.get("longitude", None),
                "altitude": pos.get("altitude", None),
                "last_heard": data.get("lastHeard", None)
            })
        return nodes

    def get_my_node_info(self) -> Dict[str, Any]:
        if not self.is_connected():
            return {"connected": False}
        my_info = getattr(self.interface, "myInfo", None)
        my_node_num = getattr(my_info, "my_node_num", 0) if my_info else 0
        nodes = self.get_nodes()
        local_node = next((n for n in nodes if n["num"] == my_node_num), None)
        return {
            "connected": True,
            "transport": "tcp",
            "host": f"{self.hostname}:{self.port}",
            "my_node_num": my_node_num,
            "node": local_node
        }

    def _handle_rx(self, packet: Any, interface: Any = None):
        if not self._on_message_callback:
            return
        try:
            decoded = packet.get("decoded", {})
            if "text" in decoded or decoded.get("portnum") in ("TEXT_MESSAGE_APP", 1):
                self._on_message_callback({
                    "sender": packet.get("fromId", f"!{packet.get('from', 0):08x}"),
                    "recipient": packet.get("toId", "^all"),
                    "channel": packet.get("channel", 0),
                    "text": decoded.get("text", ""),
                    "snr": packet.get("rxSnr"),
                    "rssi": packet.get("rxRssi"),
                    "timestamp": time.time()
                })
        except Exception as e:
            logger.error("Error processing Meshtastic TCP packet: %s", e)


class BLETransport(BaseMeshtasticTransport):
    """Bluetooth Low Energy transport using official meshtastic.ble_interface."""

    def __init__(self, address: Optional[str] = None):
        super().__init__()
        self.address = address
        self.interface = None

    def connect(self) -> bool:
        try:
            import meshtastic.ble_interface
            from pubsub import pub

            logger.info("Connecting to Meshtastic via BLE: %s", self.address or "Scan/Auto")
            self.interface = meshtastic.ble_interface.BLEInterface(address=self.address)
            pub.subscribe(self._handle_rx, "meshtastic.receive")
            return True
        except Exception as e:
            logger.error("Failed to connect Meshtastic BLE: %s", e)
            self.interface = None
            return False

    def disconnect(self) -> None:
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None

    def is_connected(self) -> bool:
        return self.interface is not None and getattr(self.interface, "isConnected", True)

    def send_text(self, text: str, destination: str = "^all", channel_index: int = 0) -> bool:
        if not self.is_connected():
            return False
        try:
            dest_id = destination if destination != "^all" else None
            self.interface.sendText(text, destinationId=dest_id, channelIndex=channel_index)
            return True
        except Exception as e:
            logger.error("Failed to send text over BLE: %s", e)
            return False

    def get_nodes(self) -> List[Dict[str, Any]]:
        if not self.is_connected():
            return []
        nodes = []
        raw_nodes = getattr(self.interface, "nodes", {}) or {}
        for num, data in raw_nodes.items():
            user = data.get("user", {})
            metrics = data.get("deviceMetrics", {})
            pos = data.get("position", {})
            nodes.append({
                "num": num,
                "id": user.get("id", f"!{num:08x}"),
                "long_name": user.get("longName", "Unknown Node"),
                "short_name": user.get("shortName", "????"),
                "hw_model": user.get("hwModel", "Unknown"),
                "battery": metrics.get("batteryLevel", None),
                "voltage": metrics.get("voltage", None),
                "snr": data.get("snr", None),
                "latitude": pos.get("latitude", None),
                "longitude": pos.get("longitude", None),
                "altitude": pos.get("altitude", None),
                "last_heard": data.get("lastHeard", None)
            })
        return nodes

    def get_my_node_info(self) -> Dict[str, Any]:
        return {"connected": self.is_connected(), "transport": "ble", "address": self.address}

    def _handle_rx(self, packet: Any, interface: Any = None):
        if not self._on_message_callback:
            return
        try:
            decoded = packet.get("decoded", {})
            if "text" in decoded or decoded.get("portnum") in ("TEXT_MESSAGE_APP", 1):
                self._on_message_callback({
                    "sender": packet.get("fromId", f"!{packet.get('from', 0):08x}"),
                    "recipient": packet.get("toId", "^all"),
                    "channel": packet.get("channel", 0),
                    "text": decoded.get("text", ""),
                    "snr": packet.get("rxSnr"),
                    "rssi": packet.get("rxRssi"),
                    "timestamp": time.time()
                })
        except Exception as e:
            logger.error("Error processing Meshtastic BLE packet: %s", e)


class MockTransport(BaseMeshtasticTransport):
    """
    Virtual simulated mesh for testing, offline evaluation, and development.
    Provides simulated nodes, telemetry, GPS coordinates, and message delivery.
    """

    def __init__(self):
        super().__init__()
        self._connected = True
        self._nodes = [
            {
                "num": 798556586,
                "id": "!2f9901aa",
                "long_name": "Nexus Base Station",
                "short_name": "NXUS",
                "hw_model": "RAK4631",
                "battery": 100,
                "voltage": 4.19,
                "snr": 9.8,
                "latitude": 39.2904,
                "longitude": -76.6122,
                "altitude": 42.0,
                "last_heard": int(time.time()) - 45
            },
            {
                "num": 1242741896,
                "id": "!4a12bc88",
                "long_name": "N3XYZ Field Portable",
                "short_name": "3XYZ",
                "hw_model": "T-Beam",
                "battery": 86,
                "voltage": 3.84,
                "snr": 7.2,
                "latitude": 39.2980,
                "longitude": -76.6210,
                "altitude": 55.0,
                "last_heard": int(time.time()) - 110
            },
            {
                "num": 2085965330,
                "id": "!7c554e12",
                "long_name": "Ridge Repeater Node",
                "short_name": "RDGE",
                "hw_model": "Heltec V3",
                "battery": 100,
                "voltage": 4.12,
                "snr": 11.5,
                "latitude": 39.3115,
                "longitude": -76.6401,
                "altitude": 140.0,
                "last_heard": int(time.time()) - 12
            }
        ]

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def send_text(self, text: str, destination: str = "^all", channel_index: int = 0) -> bool:
        if not self._connected:
            return False
        # Echo back simulated response or acknowledgment after short delay
        logger.info("[Mock Meshtastic] TX text='%s' to=%s ch=%s", text, destination, channel_index)
        return True

    def get_nodes(self) -> List[Dict[str, Any]]:
        # Update last heard timestamps slightly
        for n in self._nodes:
            if n["id"] == "!7c554e12":
                n["last_heard"] = int(time.time()) - 10
        return self._nodes

    def get_my_node_info(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "transport": "mock",
            "my_node_num": 798556586,
            "node": self._nodes[0]
        }

    def simulate_inbound_message(self, text: str, sender: str = "!4a12bc88", channel: int = 0):
        """Simulate an inbound message received over the virtual mesh."""
        if self._on_message_callback:
            self._on_message_callback({
                "sender": sender,
                "recipient": "^all",
                "channel": channel,
                "text": text,
                "snr": 8.5,
                "rssi": -72,
                "timestamp": time.time()
            })
