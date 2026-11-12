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
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager, IndentedSafeDumper, sanitize_p25_wacn


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
        conf["system"]["config"]["netId"] = sanitize_p25_wacn(conf["system"]["config"].get("netId", "BB800"))
        dvm_config_manager._heal_yaml_dict(conf)

        # Write files
        import yaml
        (target_dir / "config.yml").write_text(yaml.dump(conf, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False), encoding="utf-8")
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
        dvm_config_manager._heal_yaml_dict(conf)

        import yaml
        (target_dir / "config.yml").write_text(yaml.dump(conf, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False), encoding="utf-8")
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

            # Step 4: Copy profile files into active location safely and run safety-net sanitization
            try:
                for fname in ["config.yml", "iden_table.dat", "talkgroup_rules.yml", "rid_acl.dat"]:
                    src = source_dir / fname
                    if src.exists():
                        try:
                            backup_manager.atomic_write(self.dvm_dir / fname, src.read_text(encoding="utf-8"))
                        except UnicodeDecodeError:
                            shutil.copy2(src, self.dvm_dir / fname)

                # Sanitize active config.yml through DVMConfigManager (heals BEE00 WACN, hex channelNo, Linux log paths, UART mode, RPC ports)
                active_conf = dvm_config_manager.load_config_dict()
                dvm_config_manager.save_config_dict(active_conf, summary=f"Activate profile '{prof['name']}' ({prof['protocol'].upper()})")
                # Sync sanitized config back into the profile directory so it remains clean
                try:
                    backup_manager.atomic_write(source_dir / "config.yml", dvm_config_manager.config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
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

    def get_profile_by_protocol(self, protocol: str) -> Optional[Dict[str, Any]]:
        """Find the most appropriate profile for a protocol (dmr or p25)."""
        protocol = protocol.lower().strip()
        profiles = self.list_profiles()
        # Look for matching active profile first
        for p in profiles:
            if p["protocol"].lower() == protocol and p.get("is_active"):
                return p
        # Look for default named profile
        default_id = f"default_{protocol}"
        for p in profiles:
            if p["id"] == default_id:
                return p
        # Look for any matching profile
        for p in profiles:
            if p["protocol"].lower() == protocol:
                return p
        return None

    def sync_protocol_settings(self, protocol: str, updater_fn) -> bool:
        """
        Safely update a protocol profile's config.yml on disk without affecting
        the live radio if that protocol is currently in standby.
        updater_fn receives conf dict and modifies it in place.
        """
        import yaml
        protocol = protocol.lower().strip()
        prof = self.get_profile_by_protocol(protocol)
        prof_id = prof["id"] if prof else f"default_{protocol}"
        prof_dir = self.profiles_dir / prof_id
        prof_config = prof_dir / "config.yml"

        if not prof_config.exists():
            # If profile dir doesn't exist yet, seed it
            if protocol == "dmr":
                self.create_default_dmr_profile()
            elif protocol == "p25":
                self.create_default_p25_profile()

        if prof_config.exists():
            try:
                conf = yaml.safe_load(prof_config.read_text(encoding="utf-8")) or {}
                updater_fn(conf)
                # Enforce that inside its own profile, this protocol is enabled and the other is disabled
                conf.setdefault("protocols", {})
                if protocol == "dmr":
                    conf["protocols"].setdefault("dmr", {})["enable"] = True
                    conf["protocols"].setdefault("p25", {})["enable"] = False
                elif protocol == "p25":
                    conf["protocols"].setdefault("p25", {})["enable"] = True
                    conf["protocols"].setdefault("dmr", {})["enable"] = False
                dvm_config_manager._heal_yaml_dict(conf)
                backup_manager.atomic_write(prof_config, yaml.dump(conf, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False))
                if prof_dir.exists() and dvm_config_manager.iden_path.exists():
                    try:
                        shutil.copy2(dvm_config_manager.iden_path, prof_dir / "iden_table.dat")
                    except Exception:
                        pass
                return True
            except Exception:
                return False
        return False

    def switch_protocol_mode(self, target_protocol: str) -> Dict[str, Any]:
        """
        High-level safe protocol switcher between DMR and P25:
        1. Auto-saves current active protocol parameters into current profile.
        2. Activates the target protocol's profile atomically with rollback guard.
        """
        target = target_protocol.lower().strip()
        if target not in ["dmr", "p25"]:
            return {"success": False, "error": f"Invalid radio protocol '{target_protocol}'. Must be 'dmr' or 'p25'."}

        active_proto = dvm_config_manager.get_active_protocol()
        if active_proto == target:
            return {"success": True, "message": f"Radio is already actively running {target.upper()}."}

        # Step 1: Save current active config to its matching profile first
        curr_prof = self.get_profile_by_protocol(active_proto)
        if curr_prof:
            curr_dir = self.profiles_dir / curr_prof["id"]
            if curr_dir.exists():
                for fname in ["config.yml", "iden_table.dat", "talkgroup_rules.yml", "rid_acl.dat"]:
                    src = self.dvm_dir / fname
                    if src.exists():
                        try:
                            shutil.copy2(src, curr_dir / fname)
                        except Exception:
                            pass

        # Step 2: Identify target profile
        target_prof = self.get_profile_by_protocol(target)
        if not target_prof:
            # Create default if missing
            if target == "dmr":
                self.create_default_dmr_profile()
            elif target == "p25":
                self.create_default_p25_profile()
            target_prof = self.get_profile_by_protocol(target)

        if not target_prof:
            return {"success": False, "error": f"Could not find or create a profile for {target.upper()}."}

        # Step 3: Activate target profile
        res = self.activate_profile(target_prof["id"])
        if res.get("success"):
            log_audit("PROTOCOL_SWITCH", "SUCCESS", f"Switched radio protocol from {active_proto.upper()} to {target.upper()}")
        return res


profile_manager = ProfileManager()
