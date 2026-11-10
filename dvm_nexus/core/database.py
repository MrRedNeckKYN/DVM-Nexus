"""
DVM Nexus - SQLite Database Layer for Profiles, Revisions, and Audit Logging
"""

import sqlite3
import json
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
