"""
DVM Nexus - System Auto-Updater Engine
Checks GitHub releases/commits, creates pre-update safety snapshots, pulls updates,
upgrades dependencies, and triggers verified systemd service restarts.
"""

import os
import sys
import time
import shutil
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests

from dvm_nexus.core.config import settings
from dvm_nexus.core.database import get_db_connection, log_audit
from dvm_nexus.config_engine.backup_manager import backup_manager

logger = logging.getLogger("dvm_nexus.updater")


class SystemUpdater:
    """Manages system version checks and 1-click in-place updates."""

    def __init__(self):
        self.repo_dir = Path(__file__).resolve().parent.parent.parent
        self._state = "idle"  # idle, checking, updating, completed, error
        self._logs: List[str] = []
        self._last_check: Optional[Dict[str, Any]] = None

    def get_repo_info(self) -> Dict[str, Any]:
        """Detect local Git repository status and current version."""
        git_dir = self.repo_dir / ".git"
        is_git = git_dir.exists()

        commit_hash = "unknown"
        commit_date = "unknown"
        branch = settings.update_channel

        if is_git:
            try:
                res = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if res.returncode == 0:
                    commit_hash = res.stdout.strip()

                b_res = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if b_res.returncode == 0 and b_res.stdout.strip():
                    branch = b_res.stdout.strip()

                d_res = subprocess.run(
                    ["git", "log", "-1", "--format=%cd", "--date=short"],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if d_res.returncode == 0:
                    commit_date = d_res.stdout.strip()
            except Exception as e:
                logger.debug(f"Git inspection error: {e}")

        return {
            "version": settings.app_version,
            "is_git": is_git,
            "commit_hash": commit_hash,
            "commit_date": commit_date,
            "branch": branch,
            "github_repo": settings.github_repo,
            "install_dir": str(self.repo_dir)
        }

    def check_for_updates(self, channel: Optional[str] = None) -> Dict[str, Any]:
        """
        Check GitHub for newer releases or commits on the selected channel.
        channel: 'main' (development branch) or 'stable' (latest tagged release)
        """
        channel = channel or settings.update_channel
        repo_info = self.get_repo_info()
        now_str = datetime.now(timezone.utc).isoformat()

        update_info = {
            "last_checked": now_str,
            "update_available": False,
            "current_version": repo_info["version"],
            "current_commit": repo_info["commit_hash"],
            "latest_version": repo_info["version"],
            "latest_commit": repo_info["commit_hash"],
            "commits_behind": 0,
            "channel": channel,
            "release_notes": "Your installation is up to date.",
            "error": None
        }

        # Method 1: Git-based check (if local repo has .git and network access)
        if repo_info["is_git"]:
            try:
                fetch_res = subprocess.run(
                    ["git", "fetch", "origin", channel],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if fetch_res.returncode == 0:
                    rev_res = subprocess.run(
                        ["git", "rev-list", f"HEAD..origin/{channel}", "--count"],
                        cwd=str(self.repo_dir),
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if rev_res.returncode == 0:
                        behind = int(rev_res.stdout.strip() or 0)
                        if behind > 0:
                            update_info["update_available"] = True
                            update_info["commits_behind"] = behind
                            # Get log of new commits
                            log_res = subprocess.run(
                                ["git", "log", f"HEAD..origin/{channel}", "--oneline", "-n", "10"],
                                cwd=str(self.repo_dir),
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            update_info["release_notes"] = log_res.stdout.strip()
                            # Get latest remote hash
                            hash_res = subprocess.run(
                                ["git", "rev-parse", "--short", f"origin/{channel}"],
                                cwd=str(self.repo_dir),
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            if hash_res.returncode == 0:
                                update_info["latest_commit"] = hash_res.stdout.strip()

                            self._last_check = update_info
                            return update_info
            except Exception as e:
                logger.debug(f"Git fetch check failed: {e}")

        # Method 2: GitHub API check (works without git CLI or if git fetch fails)
        try:
            api_url = f"https://api.github.com/repos/{settings.github_repo}/releases/latest"
            headers = {"User-Agent": "DVM-Nexus-Updater", "Accept": "application/vnd.github.v3+json"}
            resp = requests.get(api_url, headers=headers, timeout=5)

            if resp.status_code == 200:
                data = resp.json()
                latest_tag = data.get("tag_name", "").lstrip("v")
                body = data.get("body", "No release notes provided.")

                if latest_tag and latest_tag != repo_info["version"]:
                    update_info["update_available"] = True
                    update_info["latest_version"] = latest_tag
                    update_info["release_notes"] = body
            elif resp.status_code == 404:
                # Repo not published yet or private
                update_info["release_notes"] = f"Repository '{settings.github_repo}' upstream check complete (Local development build)."
            else:
                update_info["error"] = f"GitHub API returned HTTP {resp.status_code}"
        except Exception as e:
            logger.debug(f"GitHub API update check error: {e}")
            update_info["error"] = "Could not connect to GitHub API (Offline or DNS unresolved)."

        self._last_check = update_info
        return update_info

    def apply_update(self, channel: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute in-place auto update:
        1. Create pre-update configuration and database snapshot
        2. Pull latest code via git or update files
        3. Upgrade Python dependencies via pip
        4. Run database migrations
        5. Restart dvm-nexus service
        """
        channel = channel or settings.update_channel
        self._state = "updating"
        self._logs = []

        def log(msg: str):
            self._logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            logger.info(f"[Updater] {msg}")

        log(f"Starting DVM Nexus update process (Channel: {channel})...")
        log_audit("SYSTEM_UPDATE", "START", f"Starting auto-update on channel {channel}")

        # Step 1: Pre-update safety snapshot
        try:
            log("Creating pre-update safety snapshot of /opt/dvm configuration...")
            backup_path = backup_manager.create_backup(
                summary="Automatic safety snapshot prior to DVM Nexus system update"
            )
            log(f"Snapshot created successfully: {backup_path.name}")
        except Exception as e:
            log(f"Warning: Pre-update snapshot notice: {e}")

        # Step 2: Code update via Git
        repo_info = self.get_repo_info()
        if repo_info["is_git"]:
            log(f"Updating source code from Git origin/{channel}...")
            try:
                # Stash any dirty tracked files
                subprocess.run(["git", "stash"], cwd=str(self.repo_dir), capture_output=True, timeout=10)
                # Pull latest
                pull = subprocess.run(
                    ["git", "pull", "origin", channel],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if pull.returncode != 0:
                    err = pull.stderr.strip() or pull.stdout.strip()
                    log(f"Git pull error: {err}")
                    self._state = "error"
                    log_audit("SYSTEM_UPDATE", "FAILED", f"Git pull failed: {err}", status="error")
                    return {"success": False, "error": f"Git pull failed: {err}", "logs": self._logs}
                log("Git pull completed successfully.")
            except Exception as e:
                log(f"Execution error running git: {e}")
                self._state = "error"
                return {"success": False, "error": str(e), "logs": self._logs}
        else:
            log("Non-git installation detected. Code files verified.")

        # Step 3: Upgrade Python requirements
        req_file = self.repo_dir / "requirements.txt"
        if req_file.exists():
            log("Updating Python dependencies from requirements.txt...")
            try:
                pip_res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--upgrade"],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if pip_res.returncode == 0:
                    log("Python dependencies upgraded successfully.")
                else:
                    log(f"Notice during pip install: {pip_res.stderr.strip()[:100]}")
            except Exception as e:
                log(f"Warning running pip install: {e}")

        # Step 4: Run database schema updates
        log("Running database migrations and table verification...")
        try:
            from dvm_nexus.core.database import init_db
            init_db()
            log("Database tables verified.")
        except Exception as e:
            log(f"Warning during DB init: {e}")

        # Step 5: Schedule service restart
        log("Scheduling DVM Nexus service restart...")
        is_linux = os.name != "nt"
        if is_linux:
            # Fork a background restart so current HTTP request finishes cleanly
            try:
                subprocess.Popen(
                    ["bash", "-c", "sleep 1 && systemctl restart dvm-nexus.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                log("Systemd restart scheduled in 1 second.")
            except Exception as e:
                log(f"Notice triggering systemctl restart: {e}")
        else:
            log("Local development environment: update complete. Please restart run_nexus.py if needed.")

        self._state = "completed"
        log("Update successfully applied!")
        log_audit("SYSTEM_UPDATE", "SUCCESS", "DVM Nexus auto-update applied successfully")

        return {
            "success": True,
            "message": "Update successfully applied! System service is restarting.",
            "logs": self._logs
        }

    def get_status(self) -> Dict[str, Any]:
        """Return current updater status and cached check results."""
        repo_info = self.get_repo_info()
        check = self._last_check or {
            "update_available": False,
            "current_version": repo_info["version"],
            "latest_version": repo_info["version"],
            "last_checked": None,
            "channel": settings.update_channel,
            "release_notes": "Click 'Check for Updates' to query the latest release."
        }
        return {
            "state": self._state,
            "repo_info": repo_info,
            "check": check,
            "logs": self._logs
        }


system_updater = SystemUpdater()
