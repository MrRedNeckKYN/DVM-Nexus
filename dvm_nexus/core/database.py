"""
DVM Nexus - SQLite Database Layer for Profiles, Revisions, and Audit Logging
"""

import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path
from dvm_nexus.core.config import settings


def get_db_connection() -> sqlite3.Connection:
    """Create and return a database connection with dict row factory."""
    conn = sqlite3.connect(str(settings.db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database tables if they do not exist."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    protocol TEXT NOT NULL CHECK(protocol IN ('dmr', 'p25')),
                    description TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{}'
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    profile_id TEXT,
                    profile_name TEXT,
                    backup_dir TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    changed_files TEXT NOT NULL,
                    restored_from INTEGER DEFAULT NULL,
                    FOREIGN KEY (profile_id) REFERENCES profiles(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT DEFAULT '',
                    status TEXT DEFAULT 'success'
                )
            """)

            # Meshtastic persistence
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meshtastic_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS meshtastic_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
                    sender_id TEXT NOT NULL,
                    sender_name TEXT DEFAULT '',
                    recipient_id TEXT DEFAULT '^all',
                    channel INTEGER DEFAULT 0,
                    text TEXT NOT NULL,
                    acknowledged INTEGER DEFAULT 0,
                    snr REAL DEFAULT NULL,
                    rssi INTEGER DEFAULT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS meshtastic_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK(source_type IN ('dmr_talkgroup', 'p25_talkgroup', 'system_status', 'inbound_meshtastic')),
                    source_id TEXT NOT NULL,
                    dest_channel INTEGER DEFAULT 0,
                    dest_node TEXT DEFAULT '^all',
                    template TEXT DEFAULT '{event_text}',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS meshtastic_admins (
                    node_id TEXT PRIMARY KEY,
                    callsign_or_name TEXT NOT NULL,
                    permission_level TEXT DEFAULT 'read' CHECK(permission_level IN ('read', 'operator', 'admin')),
                    can_restart_service INTEGER DEFAULT 0,
                    can_switch_profile INTEGER DEFAULT 0,
                    added_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS meshtastic_tokens (
                    token TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            # Bidirectional Radio <-> Meshtastic Text Messaging Tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radio_text_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    protocol TEXT NOT NULL CHECK(protocol IN ('dmr', 'p25')),
                    direction TEXT NOT NULL CHECK(direction IN ('radio_to_mesh', 'mesh_to_radio', 'web_to_radio', 'web_to_mesh')),
                    radio_src_id INTEGER DEFAULT 0,
                    radio_dest_id INTEGER DEFAULT 0,
                    is_group INTEGER DEFAULT 1,
                    mesh_node_id TEXT DEFAULT '^all',
                    mesh_channel INTEGER DEFAULT 0,
                    text TEXT NOT NULL,
                    status TEXT DEFAULT 'delivered'
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS text_bridge_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # Seed text bridge default config if not present
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM text_bridge_config")
            if cur.fetchone()[0] == 0:
                defaults = [
                    ("enabled", "1"),
                    ("dmr_channel", "1"),
                    ("p25_channel", "2"),
                    ("default_dmr_tg", "3100"),
                    ("default_p25_tg", "101"),
                    ("forward_all_to_active", "1"),
                    ("prefix_routing", "1")
                ]
                for k, v in defaults:
                    cur.execute("INSERT OR REPLACE INTO text_bridge_config (key, value) VALUES (?, ?)", (k, v))

            # Bidirectional Voice Bridge (STT / TTS) Tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voice_bridge_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS voice_bridge_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('radio_to_mesh', 'mesh_to_radio')),
                    protocol TEXT NOT NULL DEFAULT 'dmr',
                    radio_src_id INTEGER DEFAULT 0,
                    radio_dest_id INTEGER DEFAULT 0,
                    mesh_node_id TEXT DEFAULT '^all',
                    mesh_channel INTEGER DEFAULT 0,
                    text TEXT NOT NULL,
                    duration_sec REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'completed'
                )
            """)

            # Seed voice bridge default config if not present
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM voice_bridge_config")
            if cur.fetchone()[0] == 0:
                v_defaults = [
                    ("enabled", "0"),
                    ("stt_enabled", "1"),
                    ("tts_enabled", "1"),
                    ("protocol", "dmr"),
                    ("voice_talkgroup", "99"),
                    ("mesh_channel", "0"),
                    ("whisper_model", "tiny.en"),
                    ("tts_voice", "en_US-lessac-medium"),
                    ("udp_rx_port", "34001"),
                    ("udp_tx_port", "32001"),
                    ("udp_host", "127.0.0.1"),
                    ("echo_lockout_sec", "3.0")
                ]
                for k, v in v_defaults:
                    cur.execute("INSERT OR REPLACE INTO voice_bridge_config (key, value) VALUES (?, ?)", (k, v))

            # Seed default Meshtastic routing rules if none exist
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM meshtastic_routes")
            if cur.fetchone()[0] == 0:
                now_str = datetime.now(timezone.utc).isoformat()
                cur.execute("""
                    INSERT INTO meshtastic_routes (name, source_type, source_id, dest_channel, dest_node, template, enabled, created_at)
                    VALUES
                    ('DMR TG 3100 Activity Alert', 'dmr_talkgroup', '3100', 0, '^all', '[DMR TG 3100] {event_text}', 1, ?),
                    ('P25 TG 101 Activity Alert', 'p25_talkgroup', '101', 0, '^all', '[P25 TG 101] {event_text}', 1, ?),
                    ('DVM Host Status & Profile Alert', 'system_status', 'all', 0, '^all', '[DVM Nexus] System Event: {event_text}', 1, ?)
                """, (now_str, now_str, now_str))

            # Live Radio Activity & Last Heard Feed
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radio_last_heard (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    protocol TEXT NOT NULL CHECK(protocol IN ('dmr', 'p25', 'nxdn')),
                    direction TEXT NOT NULL CHECK(direction IN ('rf_rx', 'net_rx', 'tx')),
                    call_type TEXT NOT NULL DEFAULT 'group',
                    src_id INTEGER NOT NULL,
                    src_alias TEXT DEFAULT '',
                    dest_id INTEGER NOT NULL,
                    slot INTEGER DEFAULT NULL,
                    duration_sec REAL DEFAULT 0.0,
                    ber_percent REAL DEFAULT 0.0,
                    rssi_dbm INTEGER DEFAULT NULL
                )
            """)

            # Migration: flag simulated/demo rows so they are never mistaken for real RF traffic
            cols = [r[1] for r in conn.execute("PRAGMA table_info(radio_last_heard)").fetchall()]
            if "simulated" not in cols:
                conn.execute("ALTER TABLE radio_last_heard ADD COLUMN simulated INTEGER DEFAULT 0")
                # Retro-flag rows created by earlier versions' demo seeding / "Simulate Call" button
                conn.execute("""
                    UPDATE radio_last_heard SET simulated = 1
                    WHERE (src_id = 3120101 AND src_alias = 'N3XYZ Base')
                       OR (src_id = 3108842 AND src_alias = 'W4HAM Mobile')
                       OR (src_id = 1001 AND src_alias = 'Dispatch 1')
                       OR src_alias LIKE 'Unit %'
                """)

            # Live Connected Radios / Active Subscriber Peers Roster
            conn.execute("""
                CREATE TABLE IF NOT EXISTS connected_radios (
                    rid INTEGER NOT NULL,
                    protocol TEXT NOT NULL CHECK(protocol IN ('dmr', 'p25', 'nxdn')),
                    callsign TEXT DEFAULT '',
                    talkgroup INTEGER DEFAULT 0,
                    talkgroup_name TEXT DEFAULT '',
                    slot INTEGER DEFAULT NULL,
                    status TEXT NOT NULL DEFAULT 'Affiliated',
                    rssi_dbm INTEGER DEFAULT NULL,
                    ber_percent REAL DEFAULT NULL,
                    last_seen TEXT NOT NULL,
                    last_seen_epoch REAL NOT NULL,
                    call_start_epoch REAL DEFAULT NULL,
                    simulated INTEGER DEFAULT 0,
                    PRIMARY KEY (rid, protocol)
                )
            """)

            # Local Subscriber Callsign / Alias Lookup Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radio_aliases (
                    rid INTEGER PRIMARY KEY,
                    alias TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
    finally:
        conn.close()


def resolve_radio_alias(rid: int, fallback: str = "") -> str:
    """Resolve a human-readable Callsign/Alias for a Radio ID from SQLite or rid_acl.dat."""
    if not rid or rid <= 0:
        return fallback or "Unknown"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT alias FROM radio_aliases WHERE rid = ?", (int(rid),))
        row = cur.fetchone()
        if row and row["alias"]:
            return str(row["alias"])
    except Exception:
        pass
    finally:
        conn.close()

    # Check rid_acl.dat for optional inline comment alias (e.g. "1,96144 # Tait TP9900")
    try:
        from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
        acl_path = dvm_config_manager.rid_acl_path
        if acl_path.exists():
            for raw in acl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                comment = ""
                if "#" in line:
                    line, comment = line.split("#", 1)
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == int(rid):
                    if len(parts) >= 3 and parts[2]:
                        return parts[2]
                    if comment.strip():
                        return comment.strip()
    except Exception:
        pass

    if fallback and not fallback.startswith("Radio ") and not fallback.startswith("Net "):
        return fallback
    return fallback or f"RID {rid}"


def resolve_talkgroup_name(tgid: int) -> str:
    """Resolve talkgroup label from active talkgroup_rules.yml."""
    if not tgid or tgid <= 0:
        return ""
    try:
        from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
        rules = dvm_config_manager.load_talkgroup_rules()
        for r in rules:
            rid_val = r.get("id") or r.get("tgid") or r.get("source", {}).get("tgid")
            if rid_val is not None and int(rid_val) == int(tgid):
                return str(r.get("name") or "")
    except Exception:
        pass
    return ""


def set_radio_alias(rid: int, alias: str, notes: str = "") -> bool:
    """Set or update a persistent callsign/alias for a Radio ID."""
    if not rid or rid <= 0:
        return False
    clean_alias = str(alias or "").strip()
    now_str = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        with conn:
            if clean_alias:
                conn.execute("""
                    INSERT INTO radio_aliases (rid, alias, notes, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(rid) DO UPDATE SET
                        alias = excluded.alias,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                """, (int(rid), clean_alias, str(notes or ""), now_str))
                conn.execute("UPDATE connected_radios SET callsign = ? WHERE rid = ?", (clean_alias, int(rid)))
                conn.execute("UPDATE radio_last_heard SET src_alias = ? WHERE src_id = ?", (clean_alias, int(rid)))
            else:
                conn.execute("DELETE FROM radio_aliases WHERE rid = ?", (int(rid),))
        return True
    except Exception as e:
        logging.getLogger(__name__).error("Failed to set radio alias: %s", e)
        return False
    finally:
        conn.close()


def upsert_connected_radio(
    rid: Optional[int] = None,
    protocol: str = "p25",
    callsign: str = "",
    talkgroup: Optional[int] = None,
    slot: Optional[int] = None,
    status: str = "Affiliated",
    rssi_dbm: Optional[int] = None,
    ber_percent: Optional[float] = None,
    call_start_epoch: Optional[float] = None,
    simulated: bool = False,
    *,
    radio_id: Optional[int] = None,
    talkgroup_id: Optional[int] = None,
    touch_activity: bool = True,
) -> None:
    """Insert or update a subscriber radio in the live Connected Radios roster."""
    eff_rid = rid if rid is not None else radio_id
    if not eff_rid or int(eff_rid) <= 0:
        return
    eff_tg = talkgroup if talkgroup is not None else talkgroup_id
    proto = str(protocol or "p25").lower().strip()
    if proto not in ("dmr", "p25", "nxdn"):
        proto = "p25"

    resolved_alias = resolve_radio_alias(int(eff_rid), callsign)
    tg_val = int(eff_tg) if (eff_tg is not None and int(eff_tg) > 0) else 0
    tg_name = resolve_talkgroup_name(tg_val) if tg_val > 0 else ""
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.isoformat()
    now_epoch = now_dt.timestamp()

    conn = get_db_connection()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT callsign, talkgroup, talkgroup_name, slot, status, rssi_dbm, ber_percent, last_seen, last_seen_epoch, call_start_epoch, simulated FROM connected_radios WHERE rid = ? AND protocol = ?",
                (int(eff_rid), proto)
            )
            existing = cur.fetchone()
            if existing:
                if not resolved_alias or resolved_alias.startswith("RID ") or resolved_alias.startswith("Radio "):
                    if existing["callsign"] and not existing["callsign"].startswith("RID "):
                        resolved_alias = existing["callsign"]
                if tg_val == 0 and existing["talkgroup"]:
                    tg_val = int(existing["talkgroup"])
                    tg_name = existing["talkgroup_name"] or resolve_talkgroup_name(tg_val)
                if slot is None and existing["slot"] is not None:
                    slot = int(existing["slot"])
                if rssi_dbm is None and existing["rssi_dbm"] is not None:
                    rssi_dbm = int(existing["rssi_dbm"])
                if ber_percent is None and existing["ber_percent"] is not None:
                    ber_percent = float(existing["ber_percent"])
                if not touch_activity and existing["status"] == "Active TX":
                    status = "Active TX"
                if not touch_activity and existing["last_seen_epoch"]:
                    now_str = existing["last_seen"]
                    now_epoch = float(existing["last_seen_epoch"])
                if status == "Active TX" and call_start_epoch is None:
                    call_start_epoch = existing["call_start_epoch"] or now_epoch
                elif status != "Active TX":
                    call_start_epoch = None
            elif status == "Active TX" and call_start_epoch is None:
                call_start_epoch = now_epoch

            conn.execute("""
                INSERT INTO connected_radios (
                    rid, protocol, callsign, talkgroup, talkgroup_name, slot,
                    status, rssi_dbm, ber_percent, last_seen, last_seen_epoch,
                    call_start_epoch, simulated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rid, protocol) DO UPDATE SET
                    callsign = excluded.callsign,
                    talkgroup = excluded.talkgroup,
                    talkgroup_name = excluded.talkgroup_name,
                    slot = excluded.slot,
                    status = excluded.status,
                    rssi_dbm = excluded.rssi_dbm,
                    ber_percent = excluded.ber_percent,
                    last_seen = excluded.last_seen,
                    last_seen_epoch = excluded.last_seen_epoch,
                    call_start_epoch = excluded.call_start_epoch,
                    simulated = excluded.simulated
            """, (
                int(eff_rid), proto, resolved_alias, tg_val, tg_name, slot,
                status, rssi_dbm, ber_percent, now_str, now_epoch,
                call_start_epoch, 1 if simulated else 0
            ))
    except Exception as e:
        logging.getLogger(__name__).error("Failed to upsert connected radio: %s", e)
    finally:
        conn.close()


def set_radio_call_ended(
    rid: Optional[int] = None,
    protocol: Optional[str] = None,
    slot: Optional[int] = None,
    ber_percent: Optional[float] = None,
    rssi_dbm: Optional[int] = None,
    *,
    radio_id: Optional[int] = None,
) -> None:
    """Transition active transmitting radio(s) back to Affiliated / Recently Heard when a call finishes."""
    eff_rid = rid if rid is not None else radio_id
    proto = str(protocol).lower().strip() if protocol else None
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.isoformat()
    now_epoch = now_dt.timestamp()
    conn = get_db_connection()
    try:
        with conn:
            if eff_rid and int(eff_rid) > 0:
                if proto:
                    cur = conn.execute(
                        "SELECT rssi_dbm, ber_percent FROM connected_radios WHERE rid = ? AND protocol = ?",
                        (int(eff_rid), proto)
                    )
                else:
                    cur = conn.execute(
                        "SELECT rssi_dbm, ber_percent FROM connected_radios WHERE rid = ?",
                        (int(eff_rid),)
                    )
                row = cur.fetchone()
                eff_rssi = rssi_dbm if rssi_dbm is not None else (row["rssi_dbm"] if row else None)
                eff_ber = ber_percent if ber_percent is not None else (row["ber_percent"] if row else None)
                if proto:
                    conn.execute("""
                        UPDATE connected_radios
                        SET status = 'Affiliated',
                            call_start_epoch = NULL,
                            last_seen = ?,
                            last_seen_epoch = ?,
                            rssi_dbm = ?,
                            ber_percent = ?
                        WHERE rid = ? AND protocol = ?
                    """, (now_str, now_epoch, eff_rssi, eff_ber, int(eff_rid), proto))
                else:
                    conn.execute("""
                        UPDATE connected_radios
                        SET status = 'Affiliated',
                            call_start_epoch = NULL,
                            last_seen = ?,
                            last_seen_epoch = ?,
                            rssi_dbm = ?,
                            ber_percent = ?
                        WHERE rid = ?
                    """, (now_str, now_epoch, eff_rssi, eff_ber, int(eff_rid)))
            elif proto:
                conn.execute("""
                    UPDATE connected_radios
                    SET status = 'Affiliated',
                        call_start_epoch = NULL,
                        last_seen = ?,
                        last_seen_epoch = ?
                    WHERE protocol = ? AND status = 'Active TX'
                """, (now_str, now_epoch, proto))
    except Exception as e:
        logging.getLogger(__name__).error("Failed to end radio call state: %s", e)
    finally:
        conn.close()


def remove_connected_radio(rid: int, protocol: Optional[str] = None) -> None:
    """Remove a deregistered subscriber from the live Connected Radios roster."""
    if not rid or int(rid) <= 0:
        return
    conn = get_db_connection()
    try:
        with conn:
            if protocol:
                conn.execute("DELETE FROM connected_radios WHERE rid = ? AND protocol = ?", (int(rid), protocol.lower()))
            else:
                conn.execute("DELETE FROM connected_radios WHERE rid = ?", (int(rid),))
    except Exception as e:
        logging.getLogger(__name__).error("Failed to remove connected radio: %s", e)
    finally:
        conn.close()


def clear_connected_radios(simulated_only: bool = False) -> int:
    """Clear the Connected Radios roster (or only simulated entries)."""
    conn = get_db_connection()
    try:
        with conn:
            if simulated_only:
                cur = conn.execute("DELETE FROM connected_radios WHERE COALESCE(simulated, 0) = 1")
            else:
                cur = conn.execute("DELETE FROM connected_radios")
            return cur.rowcount
    finally:
        conn.close()


def get_connected_radios(limit: int = 50, active_tx_watchdog_sec: float = 30.0) -> List[Dict[str, Any]]:
    """
    Return the live Connected Radios / Active Peers roster enriched with relative
    activity indicators ('Active Now', 'Idle 2m ago') and live elapsed TX timers.
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    conn = get_db_connection()
    try:
        with conn:
            # Auto-decay any stuck 'Active TX' older than watchdog back to 'Affiliated'
            conn.execute("""
                UPDATE connected_radios
                SET status = 'Affiliated', call_start_epoch = NULL
                WHERE status = 'Active TX' AND (? - last_seen_epoch) > ?
            """, (now_epoch, active_tx_watchdog_sec))

            cur = conn.cursor()
            cur.execute("""
                SELECT rid, protocol, callsign, talkgroup, talkgroup_name, slot,
                       status, rssi_dbm, ber_percent, last_seen, last_seen_epoch,
                       call_start_epoch, COALESCE(simulated, 0) AS simulated
                FROM connected_radios
                ORDER BY
                    CASE
                        WHEN status = 'Active TX' THEN 0
                        WHEN status = 'Affiliated' THEN 1
                        WHEN status = 'Registered' THEN 2
                        ELSE 3
                    END ASC,
                    last_seen_epoch DESC
                LIMIT ?
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    enriched: List[Dict[str, Any]] = []
    for r in rows:
        age = max(0.0, now_epoch - float(r.get("last_seen_epoch") or now_epoch))
        is_tx = (r.get("status") == "Active TX")
        elapsed_sec = 0
        elapsed_str = ""
        if is_tx:
            start_ep = float(r.get("call_start_epoch") or r.get("last_seen_epoch") or now_epoch)
            elapsed_sec = max(1, int(now_epoch - start_ep))
            mins, secs = divmod(elapsed_sec, 60)
            elapsed_str = f"{mins}:{secs:02d}"
            activity_label = "Active Now"
            activity_state = "tx"
        elif age < 15.0:
            activity_label = "Active Now"
            activity_state = "active"
        elif age < 60.0:
            activity_label = f"Idle {int(age)}s ago"
            activity_state = "recent"
        elif age < 3600.0:
            activity_label = f"Idle {int(age // 60)}m ago"
            activity_state = "idle"
        elif age < 86400.0:
            activity_label = f"Idle {int(age // 3600)}h ago"
            activity_state = "stale"
        else:
            activity_label = f"Idle {int(age // 86400)}d ago"
            activity_state = "stale"

        r["radio_id"] = int(r["rid"])
        r["protocol"] = str(r.get("protocol") or "p25").upper()
        r["talkgroup_id"] = int(r["talkgroup"]) if r.get("talkgroup") else None
        r["age_sec"] = round(age, 1)
        r["is_tx"] = is_tx
        r["is_transmitting"] = is_tx
        r["elapsed_sec"] = elapsed_sec
        r["elapsed_str"] = elapsed_str
        r["tx_elapsed_display"] = elapsed_str
        r["activity_label"] = activity_label
        r["timeout_label"] = activity_label
        r["activity_state"] = activity_state
        r["last_activity_utc"] = r.get("last_seen") or ""
        if not r.get("talkgroup_name") and r.get("talkgroup"):
            r["talkgroup_name"] = resolve_talkgroup_name(int(r["talkgroup"]))
        if not r.get("callsign"):
            r["callsign"] = resolve_radio_alias(int(r["rid"]))
        enriched.append(r)

    return enriched


def record_last_heard(
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
    simulated: bool = False
) -> int:
    """Record a radio call event in the persistent Last Heard table."""
    conn = get_db_connection()
    try:
        now_str = datetime.now(timezone.utc).isoformat()
        with conn:
            cur = conn.execute("""
                INSERT INTO radio_last_heard (
                    timestamp, protocol, direction, call_type, src_id, src_alias, dest_id, slot,
                    duration_sec, ber_percent, rssi_dbm, simulated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str, protocol.lower(), direction.lower(), call_type.lower(),
                int(src_id), str(src_alias or ""), int(dest_id), slot,
                float(duration_sec), float(ber_percent or 0.0), rssi_dbm, 1 if simulated else 0
            ))
            return cur.lastrowid
    except Exception as e:
        logging.getLogger(__name__).error("Failed to record Last Heard call: %s", e)
        return 0
    finally:
        conn.close()


def get_recent_last_heard(limit: int = 30) -> List[Dict[str, Any]]:
    """Retrieve recent Last Heard radio activity ordered newest first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, protocol, direction, call_type, src_id, src_alias, dest_id, slot,
                   duration_sec, ber_percent, rssi_dbm, COALESCE(simulated, 0) AS simulated
            FROM radio_last_heard
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def clear_simulated_last_heard() -> int:
    """Delete all simulated/demo Last Heard rows. Returns number of rows removed."""
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM radio_last_heard WHERE COALESCE(simulated, 0) = 1")
            return cur.rowcount
    finally:
        conn.close()


def log_audit(category: str, action: str, details: str = "", status: str = "success"):
    """Record an audit log entry."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO audit_logs (timestamp, category, action, details, status)
                VALUES (?, ?, ?, ?, ?)
            """, (datetime.now(timezone.utc).isoformat(), category, action, details, status))
    except Exception as e:
        print(f"Failed to record audit log: {e}")
    finally:
        conn.close()


def get_recent_audit_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve recent audit logs."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, timestamp, category, action, details, status
            FROM audit_logs
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
