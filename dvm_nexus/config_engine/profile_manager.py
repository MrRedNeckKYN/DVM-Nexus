"""
DVM Nexus - Radio Profile Manager
Handles creation, storage, and safe atomic switching between saved DMR and P25 profiles.
Respects hardware modem mutual exclusion and ensures safe service restart with rollback.
"""

import os
import json
import shutil
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from dvm_nexus.core.config import settings
from dvm_nexus.core.database import get_db_connection, log_audit, init_db
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager


class ProfileManager:
    """Manages saved radio profiles and safe switching."""

    def __init__(self, profiles_dir: Optional[Path] = None, dvm_dir: Optional[Path] = None):
        self.profiles_dir = profiles_dir or settings.profiles_dir
        self.dvm_dir = dvm_dir or settings.dvm_dir
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._switch_lock = threading.Lock()
        # Ensure database tables exist before querying profiles
        init_db()
        self._ensure_default_profiles_exist()

    def _ensure_default_profiles_exist(self):
        """Create initial DMR and P25 profiles if either does not exist."""
        existing = self.list_profiles()
        has_dmr = any(p["id"] == "default_dmr" for p in existing)
        has_p25 = any(p["id"] == "default_p25" for p in existing)
        if not has_dmr:
            self.create_default_dmr_profile()
        if not has_p25:
            self.create_default_p25_profile()

    def create_default_dmr_profile(self):
        """Seed a clean DMR Duplex profile."""
        profile_id = "default_dmr"
        target_dir = self.profiles_dir / profile_id
        target_dir.mkdir(parents=True, exist_ok=True)

        dvm_config_manager.ensure_active_config_exists()
        conf = dvm_config_manager.load_config_dict()

        # Enforce DMR enabled, P25 disabled
        conf.setdefault("protocols", {})
        conf["protocols"].setdefault("dmr", {})["enable"] = True
        conf["protocols"].setdefault("p25", {})["enable"] = False
        conf.setdefault("system", {})["duplex"] = True
        conf["system"].setdefault("config", {})["colorCode"] = 1
        conf["system"]["config"]["dmrNetId"] = 1

        # Write files
        import yaml
        (target_dir / "config.yml").write_text(yaml.dump(conf, default_flow_style=False, sort_keys=False), encoding="utf-8")
        if dvm_config_manager.iden_path.exists():
            shutil.copy2(dvm_config_manager.iden_path, target_dir / "iden_table.dat")
        if dvm_config_manager.tg_rules_path.exists():
            shutil.copy2(dvm_config_manager.tg_rules_path, target_dir / "talkgroup_rules.yml")
        if dvm_config_manager.rid_acl_path.exists():
            shutil.copy2(dvm_config_manager.rid_acl_path, target_dir / "rid_acl.dat")

        self._upsert_db_profile(
            profile_id=profile_id,
            name="Default DMR Duplex",
            protocol="dmr",
            description="DMR Duplex Repeater/Hotspot profile with Color Code 1 and 2 timeslots",
            is_active=1
        )

    def create_default_p25_profile(self):
        """Seed a clean P25 Phase 1 profile."""
        profile_id = "default_p25"
        target_dir = self.profiles_dir / profile_id
        target_dir.mkdir(parents=True, exist_ok=True)

        dvm_config_manager.ensure_active_config_exists()
        conf = dvm_config_manager.load_config_dict()

        # Enforce P25 enabled, DMR disabled
        conf.setdefault("protocols", {})
        conf["protocols"].setdefault("dmr", {})["enable"] = False
        conf["protocols"].setdefault("p25", {})["enable"] = True
        conf.setdefault("system", {})["duplex"] = True
        conf["system"].setdefault("config", {})["nac"] = "293"
        conf["system"]["config"]["netId"] = "BB800"
        conf["system"]["config"]["sysId"] = "001"

        import yaml
        (target_dir / "config.yml").write_text(yaml.dump(conf, default_flow_style=False, sort_keys=False), encoding="utf-8")
        if dvm_config_manager.iden_path.exists():
            shutil.copy2(dvm_config_manager.iden_path, target_dir / "iden_table.dat")
        if dvm_config_manager.tg_rules_path.exists():
            shutil.copy2(dvm_config_manager.tg_rules_path, target_dir / "talkgroup_rules.yml")
        if dvm_config_manager.rid_acl_path.exists():
            shutil.copy2(dvm_config_manager.rid_acl_path, target_dir / "rid_acl.dat")

        self._upsert_db_profile(
            profile_id=profile_id,
            name="Default P25 Phase 1",
            protocol="p25",
            description="P25 Phase 1 Conventional / Repeater profile with NAC 293 and WACN BB800",
            is_active=0
        )

    def _upsert_db_profile(self, profile_id: str, name: str, protocol: str, description: str, is_active: int = 0):
        conn = get_db_connection()
        now = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO profiles (id, name, protocol, description, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        protocol=excluded.protocol,
                        description=excluded.description,
                        is_active=excluded.is_active,
                        updated_at=excluded.updated_at
                """, (profile_id, name, protocol, description, is_active, now, now))
        finally:
            conn.close()

    def list_profiles(self) -> List[Dict[str, Any]]:
        """Return list of saved profiles with active status."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, name, protocol, description, is_active, created_at, updated_at
                FROM profiles ORDER BY name ASC
            """)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Get profile metadata."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_active_profile(self) -> Optional[Dict[str, Any]]:
        """Get currently active profile."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM profiles WHERE is_active = 1 LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def save_current_as_profile(self, name: str, protocol: str, description: str = "") -> str:
        """Capture currently active /opt/dvm configuration as a new profile."""
        import uuid
        profile_id = f"prof_{uuid.uuid4().hex[:8]}"
        target_dir = self.profiles_dir / profile_id
        target_dir.mkdir(parents=True, exist_ok=True)

        dvm_config_manager.ensure_active_config_exists()
        for fname in ["config.yml", "iden_table.dat", "talkgroup_rules.yml", "rid_acl.dat"]:
            src = self.dvm_dir / fname
            if src.exists():
                shutil.copy2(src, target_dir / fname)

        self._upsert_db_profile(
            profile_id=profile_id,
            name=name,
            protocol=protocol,
            description=description,
            is_active=0
        )
        log_audit("PROFILE", "CREATE", f"Created profile '{name}' ({protocol.upper()})")
        return profile_id

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a saved profile (cannot delete active profile)."""
        prof = self.get_profile(profile_id)
        if not prof:
            return False
        if prof.get("is_active"):
            raise ValueError("Cannot delete the currently active profile")

        target_dir = self.profiles_dir / profile_id
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

        conn = get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        finally:
            conn.close()

        log_audit("PROFILE", "DELETE", f"Deleted profile '{prof.get('name')}'")
        return True

    def activate_profile(self, profile_id: str) -> Dict[str, Any]:
        """
        Switch to the specified profile with thread-safe concurrency locking:
        1. Pre-switch validation
        2. Backup current configuration
        3. Stop dvmhost safely (releasing modem serial port)
        4. Copy profile files into active directory
        5. Start dvmhost
        6. Verify successful startup; rollback if failed.
        """
        with self._switch_lock:
            prof = self.get_profile(profile_id)
            if not prof:
                return {"success": False, "error": f"Profile '{profile_id}' not found"}

            source_dir = self.profiles_dir / profile_id
            if not (source_dir / "config.yml").exists():
                return {"success": False, "error": f"Profile '{profile_id}' is missing config.yml"}

            log_audit("PROFILE", "SWITCH_START", f"Initiating switch to profile '{prof['name']}' ({prof['protocol'].upper()})")

            # Step 2: Backup current active state
            prev_backup = backup_manager.create_backup(
                summary=f"Pre-switch backup before activating '{prof['name']}'",
                profile_name=prof["name"]
            )

            # Step 3: Stop dvmhost service safely
            stop_res = service_controller.stop()
            if not stop_res.get("success"):
                log_audit("PROFILE", "SWITCH_FAILED", f"Could not stop dvmhost: {stop_res.get('message')}", status="error")
                return {"success": False, "error": f"Failed to stop dvmhost service: {stop_res.get('message')}"}

            # Step 4: Copy profile files into active location safely
            try:
                for fname in ["config.yml", "iden_table.dat", "talkgroup_rules.yml", "rid_acl.dat"]:
                    src = source_dir / fname
                    if src.exists():
                        try:
                            backup_manager.atomic_write(self.dvm_dir / fname, src.read_text(encoding="utf-8"))
                        except UnicodeDecodeError:
                            shutil.copy2(src, self.dvm_dir / fname)
            except Exception as e:
                # Restore files from backup immediately
                backup_manager.restore_from_directory(prev_backup, summary="Rollback on file copy failure")
                service_controller.start()
                return {"success": False, "error": f"Failed copying profile files: {e}"}

            # Step 5: Start dvmhost service
            start_res = service_controller.start()
            if not start_res.get("success"):
                # Rollback
                log_audit("PROFILE", "SWITCH_ROLLBACK", f"Service failed to start on new profile, rolling back", status="warning")
                backup_manager.restore_from_directory(prev_backup, summary="Rollback on service start failure")
                service_controller.start()
                return {"success": False, "error": f"dvmhost failed to start: {start_res.get('message')}. Rolled back to previous profile."}

            # Step 6: Verify service stays running
            if not service_controller.verify_running(timeout_sec=4.0):
                # Rollback on crash after startup
                log_audit("PROFILE", "SWITCH_ROLLBACK", f"Service died after start, rolling back", status="error")
                backup_manager.restore_from_directory(prev_backup, summary="Rollback on service crash")
                service_controller.restart()
                return {"success": False, "error": "dvmhost died shortly after start. Rolled back to previous configuration."}

            # Step 7: Update DB active flags
            conn = get_db_connection()
            try:
                with conn:
                    conn.execute("UPDATE profiles SET is_active = 0")
                    conn.execute("UPDATE profiles SET is_active = 1 WHERE id = ?", (profile_id,))
            finally:
                conn.close()

        log_audit("PROFILE", "SWITCH_SUCCESS", f"Activated profile '{prof['name']}' successfully")

        # Notify Meshtastic Event Bridge
        try:
            from dvm_nexus.meshtastic.bridge import meshtastic_bridge
            meshtastic_bridge.handle_radio_event(
                source_type="system_status",
                source_id="profile",
                event_text=f"Radio profile switched to '{prof['name']}' ({prof['protocol'].upper()})"
            )
        except Exception:
            pass

        return {
            "success": True,
            "profile": prof,
            "message": f"Successfully activated profile '{prof['name']}' ({prof['protocol'].upper()})"
        }


profile_manager = ProfileManager()
