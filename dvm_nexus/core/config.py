"""
DVM Nexus - Application Settings and Path Configuration
"""

import os
from pathlib import Path
from pydantic import BaseModel, Field

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_CONFIGS_DIR = DATA_DIR / "default_configs"
PROFILES_DIR = DATA_DIR / "profiles"
ACTIVE_CONFIG_DIR_DEV = DATA_DIR / "active"


class Settings(BaseModel):
    """Nexus application runtime settings."""

    app_name: str = "DVM Nexus"
    app_version: str = "1.0.0"
    debug: bool = Field(default_factory=lambda: os.getenv("NEXUS_DEBUG", "0") in ("1", "true", "True"))

    # Active DVMHost files location (/opt/dvm on standard Linux install)
    dvm_dir: Path = Field(
        default_factory=lambda: (
            Path("/opt/dvm")
            if os.path.exists("/opt/dvm/config.yml") or os.name != "nt"
            else ACTIVE_CONFIG_DIR_DEV
        )
    )

    # Backup and Profile directories
    backup_dir: Path = Field(default_factory=lambda: DATA_DIR / "backups")
    profiles_dir: Path = Field(default_factory=lambda: PROFILES_DIR)

    # SQLite DB for profiles, revision metadata, audit logs
    db_path: Path = Field(
        default_factory=lambda: Path(os.getenv("NEXUS_DB_PATH", str(DATA_DIR / "nexus.db")))
    )

    # DVMHost REST API connection
    dvmhost_api_host: str = Field(default_factory=lambda: os.getenv("DVMHOST_API_HOST", "127.0.0.1"))
    dvmhost_api_port: int = Field(default_factory=lambda: int(os.getenv("DVMHOST_API_PORT", "9990")))
    dvmhost_api_password: str = Field(default_factory=lambda: os.getenv("DVMHOST_API_PASSWORD", "PASSWORD"))
    dvmhost_service_name: str = Field(default="dvmhost.service")

    # Web UI settings
    web_host: str = Field(default_factory=lambda: os.getenv("NEXUS_HOST", "0.0.0.0"))
    web_port: int = Field(default_factory=lambda: int(os.getenv("NEXUS_PORT", "8080")))
    session_secret: str = Field(
        default_factory=lambda: os.getenv("NEXUS_SESSION_SECRET", "dvm-nexus-super-secure-secret-key-32b-min")
    )

    # GitHub repository & auto-updater settings
    github_repo: str = Field(default_factory=lambda: os.getenv("NEXUS_GITHUB_REPO", "MrRedNeckKYN/DVM-Nexus"))
    update_channel: str = Field(default_factory=lambda: os.getenv("NEXUS_UPDATE_CHANNEL", "main"))

    @property
    def config_yml_path(self) -> Path:
        return self.dvm_dir / "config.yml"

    @property
    def iden_table_path(self) -> Path:
        return self.dvm_dir / "iden_table.dat"

    @property
    def talkgroup_rules_path(self) -> Path:
        return self.dvm_dir / "talkgroup_rules.yml"

    @property
    def rid_acl_path(self) -> Path:
        return self.dvm_dir / "rid_acl.dat"


settings = Settings()

# Ensure directories exist
settings.backup_dir.mkdir(parents=True, exist_ok=True)
settings.profiles_dir.mkdir(parents=True, exist_ok=True)
if settings.dvm_dir == ACTIVE_CONFIG_DIR_DEV:
    settings.dvm_dir.mkdir(parents=True, exist_ok=True)
