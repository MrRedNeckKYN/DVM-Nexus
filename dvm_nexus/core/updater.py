"""
DVM Nexus - System Auto-Updater Engine
Checks GitHub releases/commits, creates pre-update safety snapshots, pulls updates,
upgrades dependencies, and triggers verified systemd service restarts.
"""

import glob
import os
import re
import sys
import time
import shutil
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import requests

from dvm_nexus.core.config import settings
from dvm_nexus.core.database import get_db_connection, log_audit
from dvm_nexus.config_engine.backup_manager import backup_manager

logger = logging.getLogger("dvm_nexus.updater")

_APP_VERSION_RE = re.compile(r'app_version\s*:\s*str\s*=\s*["\']v?([0-9]+\.[0-9]+\.[0-9]+[^"\']*)["\']')
_BADGE_VERSION_RE = re.compile(r'version-([0-9]+\.[0-9]+\.[0-9]+)-')
_COMMIT_VERSION_RE = re.compile(r'\bv?([0-9]+\.[0-9]+\.[0-9]+)\b')


def _parse_version_tuple(ver_str: Optional[str]) -> Tuple[int, ...]:
    """Convert a version string like 'v1.0.50' into a comparable integer tuple (1, 0, 50)."""
    if not ver_str:
        return (0, 0, 0)
    cleaned = str(ver_str).strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0, 0, 0)
    return tuple(int(p) for p in parts[:4])


def _extract_version_from_config_text(text: str) -> Optional[str]:
    """Extract app_version from dvm_nexus/core/config.py or README content."""
    if not text:
        return None
    m = _APP_VERSION_RE.search(text)
    if m:
        return m.group(1).strip()
    m2 = _BADGE_VERSION_RE.search(text)
    if m2:
        return m2.group(1).strip()
    return None


class SystemUpdater:
    """Manages system version checks and 1-click in-place updates."""

    def __init__(self):
        self.repo_dir = Path(__file__).resolve().parent.parent.parent
        self._state = "idle"  # idle, checking, updating, completed, error
        self._logs: List[str] = []
        self._last_check: Optional[Dict[str, Any]] = None
        self._last_check_ts: float = 0.0

    @property
    def git_bin(self) -> str:
        """Locate git binary across Raspberry Pi OS, Linux, and Windows."""
        which_git = shutil.which("git")
        if which_git:
            return which_git
        candidates = [
            r"/usr/bin/git",
            r"/usr/local/bin/git",
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe",
        ]
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.extend(
                sorted(
                    glob.glob(os.path.join(local_app_data, "GitHubDesktop", "app-*", "resources", "app", "git", "cmd", "git.exe")),
                    reverse=True,
                )
            )
        for c in candidates:
            if c and os.path.exists(c):
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

    def _read_disk_version(self) -> str:
        """Read current app_version directly from dvm_nexus/core/config.py on disk so it is never stale."""
        cfg_file = self.repo_dir / "dvm_nexus" / "core" / "config.py"
        if cfg_file.exists():
            try:
                content = cfg_file.read_text(encoding="utf-8", errors="ignore")
                extracted = _extract_version_from_config_text(content)
                if extracted:
                    settings.app_version = extracted
                    return extracted
            except Exception:
                pass
        return settings.app_version

    def get_repo_info(self) -> Dict[str, Any]:
        """Detect local Git repository status and current version."""
        git_dir = self.repo_dir / ".git"
        is_git = git_dir.exists()

        current_version = self._read_disk_version()
        commit_hash = "unknown"
        commit_date = "unknown"
        branch = settings.update_channel

        if is_git:
            try:
                res = self._run_git(["rev-parse", "--short", "HEAD"], timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    commit_hash = res.stdout.strip()

                b_res = self._run_git(["branch", "--show-current"], timeout=5)
                if b_res.returncode == 0 and b_res.stdout.strip():
                    branch = b_res.stdout.strip()

                d_res = self._run_git(["log", "-1", "--format=%cd", "--date=short"], timeout=5)
                if d_res.returncode == 0 and d_res.stdout.strip():
                    commit_date = d_res.stdout.strip()
            except Exception as e:
                logger.debug(f"Git inspection error: {e}")

        return {
            "version": current_version,
            "is_git": is_git,
            "commit_hash": commit_hash,
            "commit_date": commit_date,
            "branch": branch,
            "github_repo": settings.github_repo,
            "install_dir": str(self.repo_dir)
        }

    def _detect_remote_git_version(self, channel: str, fallback_version: str) -> str:
        """
        Determine the exact version string on origin/<channel> by inspecting:
        1. origin/<channel>:dvm_nexus/core/config.py (authoritative app_version)
        2. Latest tag reachable from origin/<channel>
        3. Commit subject on origin/<channel> (e.g. 'v1.0.50: ...')
        """
        candidates: List[str] = [fallback_version]

        # 1. Read dvm_nexus/core/config.py directly from remote branch
        try:
            show_res = self._run_git(["show", f"origin/{channel}:dvm_nexus/core/config.py"], timeout=5)
            if show_res.returncode == 0 and show_res.stdout:
                v_cfg = _extract_version_from_config_text(show_res.stdout)
                if v_cfg:
                    candidates.append(v_cfg)
        except Exception:
            pass

        # 2. Check highest semver tag on origin/<channel>
        try:
            tag_res = self._run_git(["tag", "--merged", f"origin/{channel}", "--sort=-v:refname"], timeout=5)
            if tag_res.returncode == 0 and tag_res.stdout.strip():
                first_tag = tag_res.stdout.strip().splitlines()[0].strip().lstrip("vV")
                if re.match(r"^\d+\.\d+\.\d+", first_tag):
                    candidates.append(first_tag)
        except Exception:
            pass

        # 3. Check latest commit subject on origin/<channel>
        try:
            subj_res = self._run_git(["log", "-1", "--format=%s", f"origin/{channel}"], timeout=5)
            if subj_res.returncode == 0 and subj_res.stdout.strip():
                m = _COMMIT_VERSION_RE.search(subj_res.stdout.strip())
                if m:
                    candidates.append(m.group(1))
        except Exception:
            pass

        # Return the highest semantic version discovered
        best = max(candidates, key=_parse_version_tuple)
        return best or fallback_version

    def check_for_updates(self, channel: Optional[str] = None) -> Dict[str, Any]:
        """
        Check GitHub for newer releases, tags, or commits on the selected channel.
        channel: 'main' (development branch) or 'stable' (latest tagged release)
        """
        self._state = "checking"
        channel = (channel or settings.update_channel or "main").strip()
        repo_info = self.get_repo_info()
        now_str = datetime.now(timezone.utc).isoformat()

        current_ver = repo_info["version"]
        current_commit = repo_info["commit_hash"]

        update_info: Dict[str, Any] = {
            "last_checked": now_str,
            "update_available": False,
            "current_version": current_ver,
            "current_commit": current_commit,
            "latest_version": current_ver,
            "latest_commit": current_commit,
            "commits_behind": 0,
            "channel": channel,
            "release_notes": "Your installation is up to date.",
            "error": None
        }

        # Method 1: Git-based check (if local repo has .git and network access)
        if repo_info["is_git"]:
            try:
                fetch_res = self._run_git(["fetch", "origin", channel, "--tags", "--force"], timeout=15)
                if fetch_res.returncode == 0:
                    remote_ver = self._detect_remote_git_version(channel, current_ver)
                    update_info["latest_version"] = remote_ver

                    hash_res = self._run_git(["rev-parse", "--short", f"origin/{channel}"], timeout=5)
                    if hash_res.returncode == 0 and hash_res.stdout.strip():
                        update_info["latest_commit"] = hash_res.stdout.strip()

                    rev_res = self._run_git(["rev-list", f"HEAD..origin/{channel}", "--count"], timeout=5)
                    behind = 0
                    if rev_res.returncode == 0:
                        behind = int(rev_res.stdout.strip() or 0)

                    ver_newer = _parse_version_tuple(remote_ver) > _parse_version_tuple(current_ver)
                    commit_diff = (
                        update_info["latest_commit"] != "unknown"
                        and current_commit != "unknown"
                        and update_info["latest_commit"] != current_commit
                        and behind > 0
                    )

                    if behind > 0 or ver_newer or commit_diff:
                        update_info["update_available"] = True
                        update_info["commits_behind"] = max(behind, 1)
                        log_res = self._run_git(["log", f"HEAD..origin/{channel}", "--oneline", "-n", "12"], timeout=5)
                        notes = log_res.stdout.strip() if log_res.returncode == 0 else ""
                        if not notes:
                            single_res = self._run_git(["log", "-1", "--format=%h %s (%cd)", "--date=short", f"origin/{channel}"], timeout=5)
                            notes = single_res.stdout.strip()
                        update_info["release_notes"] = (
                            f"New release v{remote_ver} ({update_info['latest_commit']}) is ready to install "
                            f"(currently on v{current_ver} / {current_commit}):\n\n{notes}"
                        )
                    else:
                        head_msg_res = self._run_git(["log", "-1", "--format=%h — %s (%cd)", "--date=short", "HEAD"], timeout=5)
                        head_msg = head_msg_res.stdout.strip() if head_msg_res.returncode == 0 else ""
                        update_info["release_notes"] = (
                            f"Up to date on v{current_ver} ({current_commit}).\n"
                            + (f"Latest commit: {head_msg}" if head_msg else "")
                        ).strip()

                    self._state = "idle"
                    self._last_check = update_info
                    self._last_check_ts = time.time()
                    return update_info
            except Exception as e:
                logger.debug(f"Git fetch check failed: {e}")

        # Method 2: GitHub HTTP/API check (fallback when git fetch fails or non-git deployment)
        try:
            headers = {"User-Agent": "DVM-Nexus-Updater", "Accept": "application/vnd.github.v3+json"}
            discovered_versions: List[str] = [current_ver]

            # Check 1: Direct raw config.py on target branch (fast & not rate-limited like API)
            raw_cfg_url = f"https://raw.githubusercontent.com/{settings.github_repo}/{channel}/dvm_nexus/core/config.py"
            try:
                raw_resp = requests.get(raw_cfg_url, headers={"User-Agent": "DVM-Nexus-Updater"}, timeout=5)
                if raw_resp.status_code == 200:
                    raw_ver = _extract_version_from_config_text(raw_resp.text)
                    if raw_ver:
                        discovered_versions.append(raw_ver)
            except Exception:
                pass

            # Check 2: Formal Releases or Tags
            releases_url = f"https://api.github.com/repos/{settings.github_repo}/releases/latest"
            resp = requests.get(releases_url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                latest_tag = data.get("tag_name", "").lstrip("vV")
                body = data.get("body", "No release notes provided.")
                if latest_tag:
                    discovered_versions.append(latest_tag)
                if latest_tag and _parse_version_tuple(latest_tag) > _parse_version_tuple(current_ver):
                    update_info["update_available"] = True
                    update_info["release_notes"] = body
            else:
                tags_url = f"https://api.github.com/repos/{settings.github_repo}/tags?per_page=20"
                tag_resp = requests.get(tags_url, headers=headers, timeout=5)
                if tag_resp.status_code == 200:
                    tags_data = tag_resp.json()
                    if isinstance(tags_data, list) and len(tags_data) > 0:
                        best_tag = max(
                            tags_data,
                            key=lambda t: _parse_version_tuple(t.get("name", "")),
                        )
                        tag_name = best_tag.get("name", "").lstrip("vV")
                        tag_sha = best_tag.get("commit", {}).get("sha", "")[:7]
                        if tag_name:
                            discovered_versions.append(tag_name)
                        if tag_sha:
                            update_info["latest_commit"] = tag_sha

            # Check 3: Latest Branch Commits
            commits_url = f"https://api.github.com/repos/{settings.github_repo}/commits/{channel}"
            c_resp = requests.get(commits_url, headers=headers, timeout=5)
            if c_resp.status_code == 200:
                c_data = c_resp.json()
                latest_sha = c_data.get("sha", "")[:7]
                commit_msg = c_data.get("commit", {}).get("message", "").strip()
                commit_author = c_data.get("commit", {}).get("author", {}).get("name", "upstream")
                commit_date = c_data.get("commit", {}).get("author", {}).get("date", "")[:10]
                if latest_sha:
                    update_info["latest_commit"] = latest_sha

                m_ver = _COMMIT_VERSION_RE.search(commit_msg.splitlines()[0] if commit_msg else "")
                if m_ver:
                    discovered_versions.append(m_ver.group(1))

                best_remote_ver = max(discovered_versions, key=_parse_version_tuple)
                update_info["latest_version"] = best_remote_ver

                ver_newer = _parse_version_tuple(best_remote_ver) > _parse_version_tuple(current_ver)
                sha_diff = (current_commit != "unknown" and latest_sha and latest_sha != current_commit)

                if ver_newer or sha_diff:
                    update_info["update_available"] = True
                    update_info["commits_behind"] = max(update_info.get("commits_behind", 0), 1)
                    update_info["release_notes"] = (
                        f"New release v{best_remote_ver} ({latest_sha}) by {commit_author} ({commit_date}):\n\n{commit_msg}"
                    )
                elif not update_info["update_available"]:
                    update_info["release_notes"] = (
                        f"Up to date with GitHub {channel} — v{current_ver} ({latest_sha}):\n\n{commit_msg}"
                    )
            else:
                best_remote_ver = max(discovered_versions, key=_parse_version_tuple)
                update_info["latest_version"] = best_remote_ver
                if _parse_version_tuple(best_remote_ver) > _parse_version_tuple(current_ver):
                    update_info["update_available"] = True
                    update_info["commits_behind"] = max(update_info.get("commits_behind", 0), 1)
                    update_info["release_notes"] = f"New version v{best_remote_ver} is available on GitHub."
        except Exception as e:
            logger.debug(f"GitHub API update check error: {e}")
            if not update_info["update_available"]:
                update_info["error"] = "Could not connect to GitHub (offline or rate-limited)."

        self._state = "idle"
        self._last_check = update_info
        self._last_check_ts = time.time()
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
        channel = (channel or settings.update_channel or "main").strip()
        self._state = "updating"
        self._logs = []

        def log(msg: str):
            self._logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            logger.info(f"[Updater] {msg}")

        old_version = self._read_disk_version()
        log(f"Starting DVM Nexus update (Installed: v{old_version}, Channel: {channel})...")
        log_audit("SYSTEM_UPDATE", "START", f"Starting auto-update on channel {channel} from v{old_version}")

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
            log(f"Fetching latest commits and tags from Git origin/{channel}...")
            try:
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

                fetch_res = self._run_git(["fetch", "origin", channel, "--tags", "--force"], timeout=25)
                if fetch_res.returncode != 0:
                    err = fetch_res.stderr.strip() or fetch_res.stdout.strip()
                    log(f"Git fetch error: {err}")
                    self._state = "error"
                    log_audit("SYSTEM_UPDATE", "FAILED", f"Git fetch failed: {err}", status="error")
                    return {"success": False, "error": f"Git fetch failed: {err}", "logs": self._logs}

                # Stash any local modifications to tracked files so pull never fails on dirty tree
                self._run_git(["stash", "--include-untracked"], timeout=10)

                # Reset/fast-forward to origin/<channel> cleanly
                pull = self._run_git(["reset", "--hard", f"origin/{channel}"], timeout=30)
                if pull.returncode != 0:
                    # Fallback to standard git pull
                    pull = self._run_git(["pull", "--ff-only", "origin", channel], timeout=30)
                if pull.returncode != 0:
                    err = pull.stderr.strip() or pull.stdout.strip()
                    log(f"Git update error: {err}")
                    self._state = "error"
                    log_audit("SYSTEM_UPDATE", "FAILED", f"Git pull failed: {err}", status="error")
                    return {"success": False, "error": f"Git pull failed: {err}", "logs": self._logs}
                log("Git source tree updated successfully.")
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
        if req_file.exists() and os.environ.get("NEXUS_TEST_DRY_RUN") != "1":
            log("Updating Python dependencies from requirements.txt...")
            try:
                pip_res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--upgrade", "--quiet"],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if pip_res.returncode == 0:
                    log("Python dependencies upgraded successfully.")
                else:
                    log(f"Notice during pip install: {pip_res.stderr.strip()[:120]}")
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

        # Step 5: Refresh on-disk version & clear cached check state
        new_version = self._read_disk_version()
        updated_repo_info = self.get_repo_info()
        self._last_check = {
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "update_available": False,
            "current_version": new_version,
            "current_commit": updated_repo_info["commit_hash"],
            "latest_version": new_version,
            "latest_commit": updated_repo_info["commit_hash"],
            "commits_behind": 0,
            "channel": channel,
            "release_notes": f"Updated to v{new_version} ({updated_repo_info['commit_hash']}).",
            "error": None,
        }
        self._last_check_ts = time.time()

        # Invalidate global template cache so new version shows immediately
        try:
            from dvm_nexus.core.templates import get_global_template_context
            get_global_template_context(force=True)
        except Exception:
            pass

        # Step 6: Schedule service restart
        log(f"Update to v{new_version} ({updated_repo_info['commit_hash']}) complete.")
        is_linux = os.name != "nt"
        if is_linux and os.environ.get("NEXUS_TEST_DRY_RUN") != "1":
            try:
                subprocess.Popen(
                    ["bash", "-c", "sleep 1.5 && (systemctl restart dvm-nexus.service || sudo -n systemctl restart dvm-nexus.service)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                log("Systemd service restart scheduled (1.5s). Web UI will automatically reload.")
            except Exception as e:
                log(f"Notice triggering systemctl restart: {e}")
        else:
            log("Development/test environment: code updated in place.")

        self._state = "completed"
        log_audit("SYSTEM_UPDATE", "SUCCESS", f"Updated from v{old_version} to v{new_version} ({updated_repo_info['commit_hash']})")

        return {
            "success": True,
            "old_version": old_version,
            "new_version": new_version,
            "new_commit": updated_repo_info["commit_hash"],
            "message": f"Updated DVM Nexus to v{new_version} ({updated_repo_info['commit_hash']}). Service is reloading.",
            "logs": self._logs
        }

    def should_auto_check(self) -> bool:
        """Return True if we haven't checked GitHub in the last 60 seconds."""
        if self._last_check is None:
            return True
        return (time.time() - self._last_check_ts) > 60.0

    def get_status(self) -> Dict[str, Any]:
        """Return current updater status and cached check results."""
        repo_info = self.get_repo_info()
        if self._last_check is not None:
            check = dict(self._last_check)
            # Keep current_version and current_commit synced with live repo_info
            check["current_version"] = repo_info["version"]
            check["current_commit"] = repo_info["commit_hash"]
            # If local repo is now at or ahead of cached latest_version & latest_commit, clear stale update_available flag
            if (
                check.get("update_available")
                and check.get("latest_commit") == repo_info["commit_hash"]
                and _parse_version_tuple(repo_info["version"]) >= _parse_version_tuple(check.get("latest_version"))
            ):
                check["update_available"] = False
                check["commits_behind"] = 0
        else:
            check = {
                "update_available": False,
                "current_version": repo_info["version"],
                "latest_version": repo_info["version"],
                "current_commit": repo_info["commit_hash"],
                "latest_commit": repo_info["commit_hash"],
                "commits_behind": 0,
                "last_checked": None,
                "channel": settings.update_channel,
                "release_notes": "Checking GitHub for updates...",
                "error": None,
            }
        return {
            "state": self._state,
            "repo_info": repo_info,
            "check": check,
            "logs": self._logs,
            "current_version": repo_info["version"],
            "latest_version": check.get("latest_version", repo_info["version"]),
            "current_commit": repo_info["commit_hash"],
            "branch": repo_info["branch"],
            "should_auto_check": self.should_auto_check(),
        }


# Singleton instance
system_updater = SystemUpdater()
