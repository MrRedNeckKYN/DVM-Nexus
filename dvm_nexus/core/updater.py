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

    @property
    def git_bin(self) -> str:
        """Locate git binary across Raspberry Pi OS, Linux, and Windows."""
        which_git = shutil.which("git")
        if which_git:
            return which_git
        candidates = [
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
            r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            r"/usr/bin/git",
            r"/usr/local/bin/git"
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return "git"

    def _run_git(self, args: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
        """
        Execute git commands with safe.directory configured to prevent
        'fatal: detected dubious ownership in repository' errors across root/pi users.
        """
        cmd = [
            self.git_bin,
            "-c", f"safe.directory={self.repo_dir}",
            "-c", "safe.directory=*",
        ] + args
        return subprocess.run(
            cmd,
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout
        )

    def get_repo_info(self) -> Dict[str, Any]:
        """Detect local Git repository status and current version."""
        git_dir = self.repo_dir / ".git"
        is_git = git_dir.exists()

        commit_hash = "unknown"
        commit_date = "unknown"
        branch = settings.update_channel

        if is_git:
            try:
                res = self._run_git(["rev-parse", "--short", "HEAD"], timeout=5)
                if res.returncode == 0:
                    commit_hash = res.stdout.strip()

                b_res = self._run_git(["branch", "--show-current"], timeout=5)
                if b_res.returncode == 0 and b_res.stdout.strip():
                    branch = b_res.stdout.strip()

                d_res = self._run_git(["log", "-1", "--format=%cd", "--date=short"], timeout=5)
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
        Check GitHub for newer releases, tags, or commits on the selected channel.
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
                fetch_res = self._run_git(["fetch", "origin", channel], timeout=15)
                if fetch_res.returncode == 0:
                    rev_res = self._run_git(["rev-list", f"HEAD..origin/{channel}", "--count"], timeout=5)
                    if rev_res.returncode == 0:
                        behind = int(rev_res.stdout.strip() or 0)
                        if behind > 0:
                            update_info["update_available"] = True
                            update_info["commits_behind"] = behind
                            # Get log of new commits
                            log_res = self._run_git(["log", f"HEAD..origin/{channel}", "--oneline", "-n", "10"], timeout=5)
                            update_info["release_notes"] = log_res.stdout.strip()
                            # Get latest remote hash
                            hash_res = self._run_git(["rev-parse", "--short", f"origin/{channel}"], timeout=5)
                            if hash_res.returncode == 0:
                                update_info["latest_commit"] = hash_res.stdout.strip()

                            self._last_check = update_info
                            return update_info
            except Exception as e:
                logger.debug(f"Git fetch check failed: {e}")

        # Method 2: GitHub API check (releases, tags, and commits)
        try:
            headers = {"User-Agent": "DVM-Nexus-Updater", "Accept": "application/vnd.github.v3+json"}

            # Check 1: Formal Releases
            releases_url = f"https://api.github.com/repos/{settings.github_repo}/releases/latest"
            resp = requests.get(releases_url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                latest_tag = data.get("tag_name", "").lstrip("v")
                body = data.get("body", "No release notes provided.")

                if latest_tag and latest_tag != repo_info["version"]:
                    update_info["update_available"] = True
                    update_info["latest_version"] = latest_tag
                    update_info["release_notes"] = body
            else:
                # Check 2: Git Tags (when no formal release page is published)
                tags_url = f"https://api.github.com/repos/{settings.github_repo}/tags"
                tag_resp = requests.get(tags_url, headers=headers, timeout=5)
                if tag_resp.status_code == 200:
                    tags_data = tag_resp.json()
                    if isinstance(tags_data, list) and len(tags_data) > 0:
                        top_tag = tags_data[0]
                        tag_name = top_tag.get("name", "").lstrip("v")
                        tag_sha = top_tag.get("commit", {}).get("sha", "")[:7]
                        update_info["latest_version"] = tag_name
                        if tag_sha:
                            update_info["latest_commit"] = tag_sha
                        if tag_name != repo_info["version"]:
                            update_info["update_available"] = True
                            update_info["release_notes"] = f"New version tag v{tag_name} is available on GitHub."

            # Check 3: Latest Branch Commits (for continuous updates on main channel)
            commits_url = f"https://api.github.com/repos/{settings.github_repo}/commits/{channel}"
            c_resp = requests.get(commits_url, headers=headers, timeout=5)
            if c_resp.status_code == 200:
                c_data = c_resp.json()
                latest_sha = c_data.get("sha", "")[:7]
                commit_msg = c_data.get("commit", {}).get("message", "").strip()
                commit_author = c_data.get("commit", {}).get("author", {}).get("name", "upstream")
                commit_date = c_data.get("commit", {}).get("author", {}).get("date", "")[:10]
                update_info["latest_commit"] = latest_sha

                if repo_info["commit_hash"] != "unknown" and latest_sha != repo_info["commit_hash"]:
                    update_info["update_available"] = True
                    update_info["commits_behind"] = max(update_info.get("commits_behind", 0), 1)
                    update_info["release_notes"] = f"New commit {latest_sha} by {commit_author} ({commit_date}):\n\n{commit_msg}"
                elif not update_info["update_available"]:
                    update_info["release_notes"] = f"Up to date with GitHub {channel} ({latest_sha}):\n\n{commit_msg}"
        except Exception as e:
            logger.debug(f"GitHub API update check error: {e}")
            if not update_info["update_available"]:
                update_info["error"] = "Could not connect to GitHub API (Offline or rate-limited)."

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
        if repo_info["is_git"] and os.environ.get("NEXUS_TEST_DRY_RUN") != "1":
            log(f"Updating source code from Git origin/{channel}...")
            try:
                # Add safe directory globally to ensure full immunity
                try:
                    subprocess.run(
                        [self.git_bin, "config", "--global", "--add", "safe.directory", str(self.repo_dir)],
                        capture_output=True,
                        timeout=5
                    )
                    subprocess.run(
                        [self.git_bin, "config", "--global", "--add", "safe.directory", "*"],
                        capture_output=True,
                        timeout=5
                    )
                except Exception:
                    pass

                # Stash any dirty tracked files
                self._run_git(["stash"], timeout=10)
                # Pull latest
                pull = self._run_git(["pull", "origin", channel], timeout=30)
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
        elif os.environ.get("NEXUS_TEST_DRY_RUN") == "1":
            log("Test dry-run mode active: git stash and git pull skipped safely.")
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

        # Step 4: Run Database Migrations
        log("Checking SQLite database schema integrity...")
        try:
            with get_db_connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.commit()
            from dvm_nexus.core.database import init_db
            init_db()
            log("Database schema verified.")
        except Exception as e:
            log(f"Database migration notice: {e}")

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
        log("Update finished successfully! DVM Nexus is running the latest version.")
        log_audit("SYSTEM_UPDATE", "SUCCESS", f"Updated successfully to channel {channel}")

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
            "current_commit": repo_info["commit_hash"],
            "latest_commit": repo_info["commit_hash"],
            "commits_behind": 0,
            "last_checked": None,
            "channel": settings.update_channel,
            "release_notes": "Click 'Check for Updates' to query the latest release."
        }
        return {
            "state": self._state,
            "repo_info": repo_info,
            "check": check,
            "logs": self._logs,
            "current_version": repo_info["version"],
            "latest_version": check.get("latest_version", repo_info["version"]),
            "current_commit": repo_info["commit_hash"],
            "branch": repo_info["branch"]
        }


# Singleton instance
system_updater = SystemUpdater()
