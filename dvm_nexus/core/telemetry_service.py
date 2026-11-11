"""
DVM Nexus - Real-Time Radio Telemetry & Last Heard Engine
Aggregates live DVMHost log events, hardware registers, and REST status
into an active RF state HUD and persistent Last Heard activity stream.
"""

import os
import re
import time
import shutil
import logging
import threading
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import deque

from dvm_nexus.core.database import record_last_heard, get_recent_last_heard
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager

logger = logging.getLogger(__name__)


class TelemetryService:
    """Monitors live DVMHost transmission activity, parses RF telemetry, and tracks call state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Current Live RF State
        self._rf_state = {
            "state": "IDLE",            # IDLE, RX, TX
            "protocol": "DMR",          # DMR, P25, NXDN
            "active_slot": None,        # 1 or 2 for DMR
            "source_id": None,          # Caller RID
            "source_alias": "",         # Callsign or Label
            "dest_id": None,            # Talkgroup or RID
            "call_type": "Group",       # Group or Private
            "rssi_dbm": None,           # e.g. -82 dBm
            "ber_percent": 0.0,         # e.g. 0.3%
            "slot1_active": False,
            "slot2_active": False,
            "last_active_time": 0.0,
            # Hardware modem status from DVMHost /status
            "tx_keyed": False,
            "modem_state": "Running",
            "adc_overflow": 0,
            "dac_overflow": 0,
            "rx_overflow": 0,
            "tx_overflow": 0,
            "dmr_free_space": 560,
            "p25_free_space": 522,
        }

        # Track ongoing call start timestamps for duration calculation
        self._call_start_times: Dict[str, float] = {}

    def get_hud_status(self) -> Dict[str, Any]:
        """Return the current live RF transceiver status with automatic timeout decay."""
        with self._lock:
            now = time.time()
            # If no active frame received in last 3.5 seconds, decay back to IDLE
            if self._rf_state["state"] in ("RX", "TX") and (now - self._rf_state["last_active_time"]) > 3.5:
                self._rf_state["state"] = "IDLE"
                self._rf_state["active_slot"] = None
                self._rf_state["slot1_active"] = False
                self._rf_state["slot2_active"] = False

            # Dynamically sync active protocol from config manager if idle
            if self._rf_state["state"] == "IDLE":
                proto = dvm_config_manager.get_active_protocol()
                self._rf_state["protocol"] = proto.upper() if proto else "DMR"

            return dict(self._rf_state)

    def get_last_heard(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Retrieve recent Last Heard entries, seeding baseline sample data if empty."""
        rows = get_recent_last_heard(limit=limit)
        if not rows:
            # Seed initial clean demonstration entries so UI is not empty
            active_proto = dvm_config_manager.get_active_protocol() or "dmr"
            if active_proto == "dmr":
                record_last_heard(
                    protocol="dmr",
                    direction="rf_rx",
                    call_type="group",
                    src_id=3120101,
                    src_alias="N3XYZ Base",
                    dest_id=3100,
                    slot=1,
                    duration_sec=3.8,
                    ber_percent=0.2,
                    rssi_dbm=-78
                )
                record_last_heard(
                    protocol="dmr",
                    direction="net_rx",
                    call_type="group",
                    src_id=3108842,
                    src_alias="W4HAM Mobile",
                    dest_id=3100,
                    slot=2,
                    duration_sec=5.2,
                    ber_percent=0.0,
                    rssi_dbm=-84
                )
            else:
                record_last_heard(
                    protocol="p25",
                    direction="rf_rx",
                    call_type="group",
                    src_id=1001,
                    src_alias="Dispatch 1",
                    dest_id=101,
                    slot=None,
                    duration_sec=4.1,
                    ber_percent=0.4,
                    rssi_dbm=-75
                )
            rows = get_recent_last_heard(limit=limit)
        return rows

    def record_call_event(
        self,
        protocol: str,
        direction: str,
        src_id: int,
        dest_id: int,
        src_alias: str = "",
        call_type: str = "group",
        slot: Optional[int] = None,
        duration_sec: float = 0.0,
        ber_percent: float = 0.0,
        rssi_dbm: Optional[int] = None
    ) -> int:
        """Record completed radio transmission and update live HUD state."""
        with self._lock:
            self._rf_state.update({
                "state": "RX" if "rx" in direction.lower() else "TX",
                "protocol": protocol.upper(),
                "active_slot": slot,
                "source_id": src_id,
                "source_alias": src_alias or f"Radio {src_id}",
                "dest_id": dest_id,
                "call_type": call_type.capitalize(),
                "rssi_dbm": rssi_dbm,
                "ber_percent": ber_percent,
                "slot1_active": (slot == 1),
                "slot2_active": (slot == 2),
                "last_active_time": time.time()
            })

        return record_last_heard(
            protocol=protocol,
            direction=direction,
            call_type=call_type,
            src_id=src_id,
            src_alias=src_alias or f"Radio {src_id}",
            dest_id=dest_id,
            slot=slot,
            duration_sec=duration_sec,
            ber_percent=ber_percent,
            rssi_dbm=rssi_dbm
        )

    def simulate_rf_call(
        self,
        protocol: str = "dmr",
        direction: str = "rf_rx",
        src_id: int = 3120101,
        dest_id: int = 3100,
        slot: int = 1,
        duration_sec: float = 3.5,
        ber_percent: float = 0.2,
        rssi_dbm: int = -79
    ):
        """Simulate an active RF call for testing and demonstration."""
        alias = f"Unit {src_id % 10000}"
        return self.record_call_event(
            protocol=protocol,
            direction=direction,
            src_id=src_id,
            dest_id=dest_id,
            src_alias=alias,
            call_type="group",
            slot=slot if protocol.lower() == "dmr" else None,
            duration_sec=duration_sec,
            ber_percent=ber_percent,
            rssi_dbm=rssi_dbm
        )

    def process_log_line(self, line: str):
        """Parse a single line from DVMHost journalctl or log stream."""
        if not line:
            return

        now = time.time()

        # 1. DMR RF Call Start
        dmr_rf = re.search(r'DMR.*received RF voice.*from (\d+) to (\d+).*slot\s*=\s*([12])', line, re.I)
        if dmr_rf:
            src_id = int(dmr_rf.group(1))
            dest_id = int(dmr_rf.group(2))
            slot = int(dmr_rf.group(3))
            call_key = f"dmr_{slot}"
            self._call_start_times[call_key] = now
            with self._lock:
                self._rf_state.update({
                    "state": "RX",
                    "protocol": "DMR",
                    "active_slot": slot,
                    "source_id": src_id,
                    "source_alias": f"Radio {src_id}",
                    "dest_id": dest_id,
                    "call_type": "Group",
                    "slot1_active": (slot == 1),
                    "slot2_active": (slot == 2),
                    "last_active_time": now
                })
            return

        # 2. DMR Network Call Start
        dmr_net = re.search(r'DMR.*received (?:network|net) voice.*from (\d+) to (\d+).*slot\s*=\s*([12])', line, re.I)
        if dmr_net:
            src_id = int(dmr_net.group(1))
            dest_id = int(dmr_net.group(2))
            slot = int(dmr_net.group(3))
            call_key = f"dmr_{slot}"
            self._call_start_times[call_key] = now
            with self._lock:
                self._rf_state.update({
                    "state": "TX",
                    "protocol": "DMR",
                    "active_slot": slot,
                    "source_id": src_id,
                    "source_alias": f"Net {src_id}",
                    "dest_id": dest_id,
                    "call_type": "Group",
                    "slot1_active": (slot == 1),
                    "slot2_active": (slot == 2),
                    "last_active_time": now
                })
            return

        # 3. DMR Call End
        dmr_end = re.search(r'DMR.*call end.*duration\s*=\s*([\d\.]+)s.*BER\s*=\s*([\d\.]+)%', line, re.I)
        if dmr_end:
            duration = float(dmr_end.group(1))
            ber = float(dmr_end.group(2))
            with self._lock:
                src_id = self._rf_state.get("source_id") or 1000
                dest_id = self._rf_state.get("dest_id") or 9
                slot = self._rf_state.get("active_slot") or 1
                alias = self._rf_state.get("source_alias") or f"Radio {src_id}"
                direction = "rf_rx" if self._rf_state.get("state") == "RX" else "net_rx"
            record_last_heard(
                protocol="dmr",
                direction=direction,
                call_type="group",
                src_id=src_id,
                src_alias=alias,
                dest_id=dest_id,
                slot=slot,
                duration_sec=duration,
                ber_percent=ber,
                rssi_dbm=self._rf_state.get("rssi_dbm")
            )
            return

        # 4. P25 RF HDU
        p25_rf = re.search(r'P25.*received RF voice HDU from (\d+) to (\d+)', line, re.I)
        if p25_rf:
            src_id = int(p25_rf.group(1))
            dest_id = int(p25_rf.group(2))
            self._call_start_times["p25"] = now
            with self._lock:
                self._rf_state.update({
                    "state": "RX",
                    "protocol": "P25",
                    "active_slot": None,
                    "source_id": src_id,
                    "source_alias": f"Radio {src_id}",
                    "dest_id": dest_id,
                    "call_type": "Group",
                    "slot1_active": True,
                    "slot2_active": False,
                    "last_active_time": now
                })
            return

        # 5. P25 Network HDU
        p25_net = re.search(r'P25.*received (?:network|net) voice HDU from (\d+) to (\d+)', line, re.I)
        if p25_net:
            src_id = int(p25_net.group(1))
            dest_id = int(p25_net.group(2))
            self._call_start_times["p25"] = now
            with self._lock:
                self._rf_state.update({
                    "state": "TX",
                    "protocol": "P25",
                    "active_slot": None,
                    "source_id": src_id,
                    "source_alias": f"Net {src_id}",
                    "dest_id": dest_id,
                    "call_type": "Group",
                    "slot1_active": True,
                    "slot2_active": False,
                    "last_active_time": now
                })
            return

        # 6. P25 Call End
        p25_end = re.search(r'P25.*call end.*duration\s*=\s*([\d\.]+)s', line, re.I)
        if p25_end:
            duration = float(p25_end.group(1))
            with self._lock:
                src_id = self._rf_state.get("source_id") or 1001
                dest_id = self._rf_state.get("dest_id") or 101
                alias = self._rf_state.get("source_alias") or f"Radio {src_id}"
                ber = self._rf_state.get("ber_percent") or 0.0
                direction = "rf_rx" if self._rf_state.get("state") == "RX" else "net_rx"
            record_last_heard(
                protocol="p25",
                direction=direction,
                call_type="group",
                src_id=src_id,
                src_alias=alias,
                dest_id=dest_id,
                slot=None,
                duration_sec=duration,
                ber_percent=ber,
                rssi_dbm=self._rf_state.get("rssi_dbm")
            )
            return

        # 7. RSSI
        rssi_match = re.search(r'RSSI\s*[:=]\s*(-?\d+)', line, re.I)
        if rssi_match:
            with self._lock:
                self._rf_state["rssi_dbm"] = int(rssi_match.group(1))

        # 8. P25 error count to BER
        p25_errs = re.search(r'P25.*(?:LDU|errs).*errs\s*=\s*(\d+)/(\d+)', line, re.I)
        if p25_errs:
            errs = int(p25_errs.group(1))
            total = int(p25_errs.group(2))
            if total > 0:
                with self._lock:
                    self._rf_state["ber_percent"] = round((errs / total) * 100.0, 1)

    def _sync_hardware_status(self):
        """Poll DVMHost REST API /status for hardware buffer and transmitter state."""
        try:
            status_res = dvm_client.get_status()
            if status_res.get("success") and isinstance(status_res.get("data"), dict):
                data = status_res["data"]
                with self._lock:
                    self._rf_state["tx_keyed"] = bool(data.get("tx", False))
                    self._rf_state["adc_overflow"] = int(data.get("adcOverflow", 0))
                    self._rf_state["dac_overflow"] = int(data.get("dacOverflow", 0))
                    self._rf_state["rx_overflow"] = int(data.get("rxOverflow", 0))
                    self._rf_state["tx_overflow"] = int(data.get("txOverflow", 0))
                    if "dmrSpace1" in data:
                        self._rf_state["dmr_free_space"] = int(data["dmrSpace1"])
                    if "p25Space" in data:
                        self._rf_state["p25_free_space"] = int(data["p25Space"])
                    if self._rf_state["tx_keyed"] and self._rf_state["state"] == "IDLE":
                        self._rf_state["state"] = "TX"
                        self._rf_state["last_active_time"] = time.time()
        except Exception:
            pass

    def _worker_loop(self):
        """Background thread: tails journalctl on Linux or checks REST API periodically."""
        has_journalctl = (shutil.which("journalctl") is not None and os.name != "nt")

        if has_journalctl:
            try:
                proc = subprocess.Popen(
                    ["journalctl", "-u", "dvmhost.service", "-f", "-n", "10", "--no-pager"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1
                )
                while self._running:
                    line = proc.stdout.readline()
                    if line:
                        self.process_log_line(line)
                    else:
                        time.sleep(0.1)
                proc.terminate()
            except Exception as e:
                logger.debug(f"Journalctl monitor exception: {e}")

        # Fallback / sync loop
        while self._running:
            self._sync_hardware_status()
            time.sleep(1.0)

    def start(self):
        """Start the background telemetry monitor."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="TelemetryWorker")
        self._thread.start()

    def stop(self):
        """Stop the background telemetry monitor."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)


# Global Singleton
telemetry_service = TelemetryService()
