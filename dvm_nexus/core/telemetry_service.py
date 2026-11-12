"""
DVM Nexus - Real-Time Radio Telemetry & Last Heard Engine
Aggregates live DVMHost log events, hardware registers, and REST status
into an active RF state HUD and persistent Last Heard activity stream.

Design notes
------------
* Two independent worker threads: one tails the DVMHost journal, the other polls the
  DVMHost REST API + systemd status. A stalled journal can therefore never starve
  hardware polling (and vice versa).
* A call keeps the HUD in RX/TX until the matching "end of transmission" log line (or a
  generous watchdog expires) - long transmissions no longer flip to IDLE mid-call.
* No demo data is ever fabricated. Simulated calls are explicitly flagged in the DB.
"""

import os
import re
import time
import select
import shutil
import logging
import threading
import subprocess
from typing import Dict, Any, List, Optional, Tuple

from dvm_nexus.core.database import record_last_heard, get_recent_last_heard
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager

logger = logging.getLogger(__name__)

# Seconds an RX/TX state is held with no further activity before the watchdog forces IDLE.
CALL_WATCHDOG_SEC = 30.0
# Seconds before REST telemetry is considered stale.
HW_STALE_SEC = 10.0
# How long the active protocol read from config.yml is cached.
PROTOCOL_CACHE_SEC = 5.0

# --------------------------------------------------------------------------------------
# DVMHost log patterns.
# Upstream (MMDVMHost lineage) style:  "DMR Slot 1, received RF voice header from 3120101 to TG 3100"
#                                      "DMR Slot 1, RF end of voice transmission, 4.2 seconds, BER: 0.5%"
# Older Nexus-style lines are still accepted so existing logs and tests keep working.
# --------------------------------------------------------------------------------------
_TO = r"to\s+(?:TG\s*)?(?P<dst>\d+)"
_SRC = r"from\s+(?P<src>[A-Za-z0-9_-]+)"

_DMR_RF_START = [
    re.compile(r"DMR\s+Slot\s*(?P<slot>[12]),?\s+received\s+RF\s+voice\s+(?:header|transmission)\s+" + _SRC + r"\s+" + _TO, re.I),
    re.compile(r"DMR.*received RF voice.*" + _SRC + r" " + _TO + r".*slot\s*=\s*(?P<slot>[12])", re.I),
]
_DMR_NET_START = [
    re.compile(r"DMR\s+Slot\s*(?P<slot>[12]),?\s+received\s+network\s+voice\s+(?:header|transmission)\s+" + _SRC + r"\s+" + _TO, re.I),
    re.compile(r"DMR.*received (?:network|net) voice.*" + _SRC + r" " + _TO + r".*slot\s*=\s*(?P<slot>[12])", re.I),
]
_DMR_END = [
    re.compile(r"DMR\s+Slot\s*(?P<slot>[12]),?\s+(?:received\s+)?(?P<kind>RF|network)\s+end of voice transmission(?:,?\s+from\s+\S+\s+to\s+(?:TG\s*)?\d+)?,?\s+(?P<dur>[\d.]+)\s+seconds?(?:.*?BER:\s*(?P<ber>[\d.]+)%)?", re.I),
    re.compile(r"DMR.*call end.*duration\s*=\s*(?P<dur>[\d.]+)s.*BER\s*=\s*(?P<ber>[\d.]+)%", re.I),
]
_P25_RF_START = [
    re.compile(r"P25.*?received\s+RF\s+voice\s+(?:transmission|HDU)?.*?" + _SRC + r"\s+" + _TO, re.I),
]
_P25_NET_START = [
    re.compile(r"P25.*?received\s+(?:network|net)\s+voice\s+(?:transmission|HDU)?.*?" + _SRC + r"\s+" + _TO, re.I),
]
_P25_END = [
    re.compile(r"P25.*?(?:received\s+)?(?P<kind>RF|network)\s+end of voice transmission(?:,?\s+from\s+\S+\s+to\s+(?:TG\s*)?\d+)?,?\s+(?P<dur>[\d.]+)\s+seconds?(?:.*?BER:\s*(?P<ber>[\d.]+)%)?", re.I),
    re.compile(r"P25.*call end.*duration\s*=\s*(?P<dur>[\d.]+)s", re.I),
]
_RSSI = re.compile(r"RSSI\s*[:=]\s*(?P<rssi>-?\d+)", re.I)
_P25_ERRS = re.compile(r"P25.*(?:LDU|errs).*errs\s*=\s*(?P<errs>\d+)/(?P<total>\d+)", re.I)
_VOICE_HINT = re.compile(r"voice (?:header|transmission|HDU)|end of voice", re.I)


def _first_match(patterns: List["re.Pattern"], line: str):
    for pat in patterns:
        m = pat.search(line)
        if m:
            return m
    return None


class TelemetryService:
    """Monitors live DVMHost transmission activity, parses RF telemetry, and tracks call state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._journal_proc: Optional[subprocess.Popen] = None

        # Current Live RF State (None == "unknown / no data", never a fabricated default)
        self._rf_state: Dict[str, Any] = {
            "state": "IDLE",            # IDLE, RX, TX
            "protocol": "DMR",          # DMR, P25, NXDN
            "active_slot": None,        # 1 or 2 for DMR
            "source_id": None,          # Caller RID
            "source_alias": "",         # Callsign or Label
            "dest_id": None,            # Talkgroup or RID
            "call_type": "Group",       # Group or Private
            "rssi_dbm": None,           # e.g. -82 dBm
            "ber_percent": None,        # e.g. 0.3 %
            "slot1_active": False,
            "slot2_active": False,
            "last_active_time": 0.0,
            "hold_sec": CALL_WATCHDOG_SEC,
            # Hardware modem status from DVMHost REST /status
            "tx_keyed": False,
            "modem_state": "Unknown",
            "adc_overflow": None,
            "dac_overflow": None,
            "rx_overflow": None,
            "tx_overflow": None,
            "dmr_free_space": None,
            "p25_free_space": None,
            # Link health
            "service_running": None,    # True / False / None (unknown)
            "rest_ok": None,            # True / False / None (unknown)
            "hw_updated": 0.0,
        }

        # Track ongoing call start timestamps for duration calculation
        self._call_start_times: Dict[str, float] = {}
        self._proto_cache: Tuple[str, float] = ("DMR", 0.0)
        self._unparsed_count = 0
        self._unparsed_last_warn = 0.0
        self._last_dvm_pid: Optional[int] = None

    # ------------------------------------------------------------------ HUD / feed accessors
    def _active_protocol_cached(self) -> str:
        proto, ts = self._proto_cache
        now = time.time()
        if now - ts > PROTOCOL_CACHE_SEC:
            try:
                p = dvm_config_manager.get_active_protocol()
                proto = p.upper() if p else "DMR"
            except Exception as exc:
                logger.debug("Active protocol lookup failed: %s", exc)
            self._proto_cache = (proto, now)
        return proto

    def get_hud_status(self) -> Dict[str, Any]:
        """Return the current live RF transceiver status (watchdog decays stale calls)."""
        idle_protocol_needed = False
        with self._lock:
            now = time.time()
            st = self._rf_state
            hold = float(st.get("hold_sec") or CALL_WATCHDOG_SEC)
            if st["state"] in ("RX", "TX") and (now - st["last_active_time"]) > hold:
                self._to_idle_locked()

            if st.get("hw_updated") and (now - st["hw_updated"]) > HW_STALE_SEC:
                st["rest_ok"] = False

            idle_protocol_needed = (st["state"] == "IDLE")
            snapshot = dict(st)

        if idle_protocol_needed:
            snapshot["protocol"] = self._active_protocol_cached()
        return snapshot

    def get_last_heard(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Retrieve recent Last Heard entries (real traffic only - nothing is fabricated)."""
        return get_recent_last_heard(limit=limit)

    def get_parser_stats(self) -> Dict[str, Any]:
        """Diagnostics: count of voice-related journal lines the parser could not understand."""
        return {"unparsed_voice_lines": self._unparsed_count}

    # ------------------------------------------------------------------ state helpers
    def _to_idle_locked(self):
        st = self._rf_state
        st["state"] = "IDLE"
        st["active_slot"] = None
        st["slot1_active"] = False
        st["slot2_active"] = False

    def _begin_call(self, state: str, protocol: str, src: Any, dest_id: int,
                    slot: Optional[int], alias_prefix: str, now: float):
        key = f"{protocol.lower()}_{slot}" if slot else protocol.lower()
        src_str = str(src).strip()
        if src_str.isdigit():
            src_id = int(src_str)
            src_alias = f"{alias_prefix} {src_id}"
        else:
            src_id = 0
            src_alias = src_str
        with self._lock:
            self._call_start_times[key] = now
            self._rf_state.update({
                "state": state,
                "protocol": protocol.upper(),
                "active_slot": slot,
                "source_id": src_id,
                "source_alias": src_alias,
                "dest_id": dest_id,
                "call_type": "Group",
                "slot1_active": (slot == 1) if slot else True,
                "slot2_active": (slot == 2) if slot else False,
                "ber_percent": None,
                "last_active_time": now,
                "hold_sec": CALL_WATCHDOG_SEC,
            })

    def _end_call(self, protocol: str, slot: Optional[int], duration: float,
                  ber: Optional[float], kind_hint: Optional[str]):
        """Record a finished call from stored live state, then release the HUD."""
        with self._lock:
            st = self._rf_state
            src_id = st.get("source_id") or 0
            dest_id = st.get("dest_id") or 0
            slot = slot or st.get("active_slot")
            alias = st.get("source_alias") or f"Radio {src_id}"
            was_rx = (st.get("state") == "RX")
            if kind_hint:
                was_rx = (kind_hint.lower() == "rf")
            direction = "rf_rx" if was_rx else "net_rx"
            rssi = st.get("rssi_dbm")
            if ber is None:
                ber = st.get("ber_percent")
            if ber is not None:
                st["ber_percent"] = ber
            self._to_idle_locked()
            st["last_active_time"] = time.time()
        record_last_heard(
            protocol=protocol.lower(),
            direction=direction,
            call_type="group",
            src_id=src_id,
            src_alias=alias,
            dest_id=dest_id,
            slot=slot if protocol.lower() == "dmr" else None,
            duration_sec=duration,
            ber_percent=ber if ber is not None else 0.0,
            rssi_dbm=rssi,
        )

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
        rssi_dbm: Optional[int] = None,
        simulated: bool = False,
    ) -> int:
        """Record completed radio transmission and update live HUD state."""
        with self._lock:
            self._rf_state.update({
                "state": "RX" if direction.lower() == "rf_rx" else "TX",
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
                "last_active_time": time.time(),
                # Simulated calls only flash the HUD briefly
                "hold_sec": max(3.0, min(float(duration_sec or 3.0), 8.0)) if simulated else CALL_WATCHDOG_SEC,
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
            rssi_dbm=rssi_dbm,
            simulated=simulated,
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
        """Simulate an RF call for testing the UI. Rows are flagged as simulated."""
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
            rssi_dbm=rssi_dbm,
            simulated=True,
        )

    # ------------------------------------------------------------------ journal parsing
    def process_log_line(self, line: str):
        """Parse a single line from DVMHost journalctl or log stream."""
        if not line:
            return

        now = time.time()

        m = _first_match(_DMR_RF_START, line)
        if m:
            self._begin_call("RX", "DMR", m["src"], int(m["dst"]), int(m["slot"]), "Radio", now)
            return

        m = _first_match(_DMR_NET_START, line)
        if m:
            self._begin_call("TX", "DMR", m["src"], int(m["dst"]), int(m["slot"]), "Net", now)
            return

        m = _first_match(_DMR_END, line)
        if m:
            groups = m.groupdict()
            slot = int(groups["slot"]) if groups.get("slot") else None
            ber = float(groups["ber"]) if groups.get("ber") else None
            self._end_call("dmr", slot, float(groups["dur"]), ber, groups.get("kind"))
            return

        m = _first_match(_P25_RF_START, line)
        if m:
            self._begin_call("RX", "P25", m["src"], int(m["dst"]), None, "Radio", now)
            return

        m = _first_match(_P25_NET_START, line)
        if m:
            self._begin_call("TX", "P25", m["src"], int(m["dst"]), None, "Net", now)
            return

        m = _first_match(_P25_END, line)
        if m:
            groups = m.groupdict()
            ber = float(groups["ber"]) if groups.get("ber") else None
            self._end_call("p25", None, float(groups["dur"]), ber, groups.get("kind"))
            return

        m = _RSSI.search(line)
        if m:
            with self._lock:
                self._rf_state["rssi_dbm"] = int(m["rssi"])
                if self._rf_state["state"] in ("RX", "TX"):
                    self._rf_state["last_active_time"] = now

        m = _P25_ERRS.search(line)
        if m:
            errs, total = int(m["errs"]), int(m["total"])
            if total > 0:
                with self._lock:
                    self._rf_state["ber_percent"] = round((errs / total) * 100.0, 1)
                    if self._rf_state["state"] in ("RX", "TX"):
                        self._rf_state["last_active_time"] = now
            return

        # A voice-looking line we could not parse: surface it (rate limited) so a log-format
        # mismatch with the installed dvmhost version is discoverable instead of silent.
        if _VOICE_HINT.search(line):
            self._unparsed_count += 1
            if now - self._unparsed_last_warn > 60:
                self._unparsed_last_warn = now
                logger.warning("Telemetry parser did not understand DVMHost voice log line: %s", line.strip()[:200])

    # ------------------------------------------------------------------ hardware polling
    def _sync_hardware_status(self):
        """Poll systemd + DVMHost REST /status for hardware buffer and transmitter state."""
        running: Optional[bool]
        cur_pid: Optional[int] = None
        try:
            svc_st = service_controller.get_status()
            running = bool(svc_st.get("is_running"))
            cur_pid = svc_st.get("main_pid")
        except Exception as exc:
            logger.debug("Service status poll failed: %s", exc)
            running = None

        with self._lock:
            self._rf_state["service_running"] = running

        if running is False:
            self._last_dvm_pid = None
            dvm_client.invalidate_token()
            with self._lock:
                self._rf_state["rest_ok"] = False
                self._rf_state["modem_state"] = "Service stopped"
                if self._rf_state["state"] != "IDLE":
                    self._to_idle_locked()
            return

        if cur_pid is not None and cur_pid != self._last_dvm_pid:
            self._last_dvm_pid = cur_pid
            dvm_client.invalidate_token()

        try:
            status_res = dvm_client.get_status()
            data = status_res.get("data") if status_res.get("success") else None
            if not isinstance(data, dict):
                raise RuntimeError(status_res.get("error") or "REST API returned no data")

            with self._lock:
                st = self._rf_state
                st["rest_ok"] = True
                st["hw_updated"] = time.time()
                st["modem_state"] = "Running"
                st["tx_keyed"] = bool(data.get("tx", False))
                st["adc_overflow"] = int(data.get("adcOverflow", 0) or 0)
                st["dac_overflow"] = int(data.get("dacOverflow", 0) or 0)
                st["rx_overflow"] = int(data.get("rxOverflow", 0) or 0)
                st["tx_overflow"] = int(data.get("txOverflow", 0) or 0)
                if "dmrSpace1" in data:
                    st["dmr_free_space"] = int(data["dmrSpace1"])
                if "p25Space" in data:
                    st["p25_free_space"] = int(data["p25Space"])
                if st["tx_keyed"] and st["state"] == "IDLE":
                    st["state"] = "TX"
                    st["last_active_time"] = time.time()
                    st["hold_sec"] = 3.0
        except Exception as exc:
            logger.debug("DVMHost REST status poll failed: %s", exc)
            with self._lock:
                self._rf_state["rest_ok"] = False
                self._rf_state["modem_state"] = "Unreachable"

    def _hardware_loop(self):
        while not self._stop_event.is_set():
            try:
                self._sync_hardware_status()
            except Exception:
                logger.exception("Telemetry hardware loop error")
            self._stop_event.wait(1.0)

    def _terminate_journal(self):
        proc, self._journal_proc = self._journal_proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _journal_loop(self):
        """Tail the dvmhost journal, restarting journalctl with back-off if it ever exits."""
        if not (shutil.which("journalctl") is not None and os.name != "nt"):
            return
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                proc = subprocess.Popen(
                    ["journalctl", "-u", "dvmhost.service", "-f", "-n", "10", "--no-pager"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    errors="replace",
                    bufsize=1,
                )
                self._journal_proc = proc
                backoff = 2.0
                while not self._stop_event.is_set():
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if ready:
                        line = proc.stdout.readline()
                        if not line:
                            break  # EOF - journalctl exited
                        self.process_log_line(line)
                    elif proc.poll() is not None:
                        break
            except Exception as exc:
                logger.warning("Journal monitor error: %s", exc)
            finally:
                self._terminate_journal()
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Start the background telemetry monitors (journal tail + hardware poll)."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._threads = [
            threading.Thread(target=self._hardware_loop, daemon=True, name="TelemetryHardware"),
            threading.Thread(target=self._journal_loop, daemon=True, name="TelemetryJournal"),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        """Stop the background telemetry monitors."""
        self._running = False
        self._stop_event.set()
        self._terminate_journal()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []


# Global Singleton
telemetry_service = TelemetryService()
