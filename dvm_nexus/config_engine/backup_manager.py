"""
DVM Nexus - Safe Backup, Atomic Write, and Rollback Manager
Guarantees radio configuration files are never corrupted or lost.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from dvm_nexus.core.config import settings
from dvm_nexus.core.database import get_db_connection, log_audit


class BackupManager:
    """Manages configuration snapshots, backups, and rollbacks."""

    def __init__(self, backup_dir: Optional[Path] = None, dvm_dir: Optional[Path] = None):
        self.backup_dir = backup_dir or settings.backup_dir
        self.dvm_dir = dvm_dir or settings.dvm_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, summary: str = "Configuration update", changed_files: Optional[List[str]] = None, profile_name: str = "") -> Path:
        """Create a timestamped backup of all active configuration files."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        target_dir = self.backup_dir / stamp
        target_dir.mkdir(parents=True, exist_ok=True)

        tracked_files = ["config.yml", "iden_table.dat", "talkgroup_rules.yml", "rid_acl.dat", "peer_list.dat"]
        if changed_files:
            tracked_files = list(dict.fromkeys(tracked_files + changed_files))

        saved_files = []

        for fname in tracked_files:
            src = self.dvm_dir / fname
            if src.exists() and src.is_file():
                shutil.copy2(src, target_dir / fname)
                saved_files.append(fname)

        # Record in SQLite database
        conn = get_db_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO config_revisions (timestamp, profile_name, backup_dir, summary, changed_files)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    profile_name,
                    str(target_dir),
                    summary,
                    ", ".join(changed_files or saved_files)
                ))
        finally:
            conn.close()

        # Prune old backups to avoid SD card exhaustion
        self.prune_backups(keep=40)

        log_audit("BACKUP", "CREATE", f"Snapshot {stamp} saved ({len(saved_files)} files): {summary}")
        return target_dir

    def prune_backups(self, keep: int = 40):
        """Rotate and prune backups to preserve disk space and SD card endurance."""
        try:
            dirs = [d for d in self.backup_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
            dirs.sort(key=lambda d: d.name)
            if len(dirs) > keep:
                to_delete = dirs[:len(dirs) - keep]
                for d in to_delete:
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

    def atomic_write(self, target_path: Path, content: str):
        """Write content to target_path safely using an atomic temporary file."""
        if "\x00" in content:
            raise ValueError("Configuration content contains a null byte")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o640
        uid, gid = None, None
        if target_path.exists():
            st = target_path.stat()
            mode = st.st_mode & 0o7777
            uid, gid = st.st_uid, st.st_gid
        elif target_path.parent.exists():
            st = target_path.parent.stat()
            uid, gid = st.st_uid, st.st_gid

        fd, temp_path = tempfile.mkstemp(prefix=f".{target_path.name}.", dir=str(target_path.parent))
        temp_p = Path(temp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temp_p.chmod(mode)
            except Exception:
                pass
            if uid is not None and gid is not None and hasattr(os, "chown") and os.name != "nt":
                try:
                    os.chown(str(temp_p), uid, gid)
                except Exception:
                    pass
            os.replace(str(temp_p), str(target_path))
        except Exception:
            if temp_p.exists():
                temp_p.unlink(missing_ok=True)
            raise

    def list_revisions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List historical configuration revisions."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, timestamp, profile_name, backup_dir, summary, changed_files, restored_from
                FROM config_revisions
                ORDER BY id DESC LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def restore_from_directory(self, backup_dir: Path, summary: str = "Rollback restore") -> Dict[str, Any]:
        """Atomically restore active configuration files from a backup directory."""
        if not backup_dir.exists():
            return {"success": False, "error": f"Backup directory '{backup_dir}' does not exist"}

        restored = []
        for src in backup_dir.iterdir():
            if src.is_file() and not src.name.startswith("."):
                dest = self.dvm_dir / src.name
                try:
                    text = src.read_text(encoding="utf-8")
                    self.atomic_write(dest, text)
                except UnicodeDecodeError:
                    shutil.copy2(src, dest)
                restored.append(src.name)

        log_audit("ROLLBACK", "RESTORE", f"Restored {len(restored)} files from {backup_dir.name} ({summary})")
        return {"success": True, "restored_files": restored, "message": f"Successfully restored {len(restored)} files"}

    def rollback(self, revision_id: int) -> Dict[str, Any]:
        """Rollback active configuration to a historical revision."""
        conn = get_db_connection()
        backup_path_str = None
        summary = None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT backup_dir, summary FROM config_revisions WHERE id = ?", (revision_id,))
            row = cursor.fetchone()
            if row:
                backup_path_str = row["backup_dir"]
                summary = row["summary"]
        finally:
            conn.close()

        if not backup_path_str or not Path(backup_path_str).exists():
            return {"success": False, "error": f"Backup directory for revision #{revision_id} does not exist"}

        # First backup current state before rolling back
        self.create_backup(summary=f"Pre-rollback backup before restoring revision #{revision_id}")

        res = self.restore_from_directory(Path(backup_path_str), summary=f"Restored revision #{revision_id} ({summary})")
        if res.get("success"):
            res["message"] = f"Successfully rolled back to revision #{revision_id}"
        return res


backup_manager = BackupManager()
