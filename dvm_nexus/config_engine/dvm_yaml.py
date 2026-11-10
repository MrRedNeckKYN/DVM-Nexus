"""
DVM Nexus - DVMHost YAML & Data Table Manager
Provides structured access, modifications, and validation for DVMHost files.
"""

import os
import csv
import io
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dvm_nexus.core.config import settings, DEFAULT_CONFIGS_DIR
from dvm_nexus.config_engine.backup_manager import backup_manager


class DVMConfigManager:
    """Manages active DVMHost configuration files."""

    def __init__(self, dvm_dir: Optional[Path] = None):
        self.dvm_dir = dvm_dir or settings.dvm_dir

    @property
    def config_path(self) -> Path:
        return self.dvm_dir / "config.yml"

    @property
    def iden_path(self) -> Path:
        return self.dvm_dir / "iden_table.dat"

    @property
    def tg_rules_path(self) -> Path:
        return self.dvm_dir / "talkgroup_rules.yml"

    @property
    def rid_acl_path(self) -> Path:
        return self.dvm_dir / "rid_acl.dat"

    def ensure_active_config_exists(self):
        """Ensure active directory has baseline configuration files."""
        self.dvm_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            dmr_src = PROFILES_DIR / "default_dmr" / "config.yml"
            if dmr_src.exists():
                backup_manager.atomic_write(self.config_path, dmr_src.read_text(encoding="utf-8"))
            else:
                example = DEFAULT_CONFIGS_DIR / "config.example.yml"
                if example.exists():
                    backup_manager.atomic_write(self.config_path, example.read_text(encoding="utf-8"))

        if not self.iden_path.exists():
            dmr_iden = PROFILES_DIR / "default_dmr" / "iden_table.dat"
            src = dmr_iden if dmr_iden.exists() else (DEFAULT_CONFIGS_DIR / "iden_table.example.dat")
            if src.exists():
                backup_manager.atomic_write(self.iden_path, src.read_text(encoding="utf-8"))

        if not self.tg_rules_path.exists():
            dmr_tg = PROFILES_DIR / "default_dmr" / "talkgroup_rules.yml"
            src = dmr_tg if dmr_tg.exists() else (DEFAULT_CONFIGS_DIR / "talkgroup_rules.example.yml")
            if src.exists():
                backup_manager.atomic_write(self.tg_rules_path, src.read_text(encoding="utf-8"))

        if not self.rid_acl_path.exists():
            dmr_rid = PROFILES_DIR / "default_dmr" / "rid_acl.dat"
            src = dmr_rid if dmr_rid.exists() else (DEFAULT_CONFIGS_DIR / "rid_acl.example.dat")
            if src.exists():
                backup_manager.atomic_write(self.rid_acl_path, src.read_text(encoding="utf-8"))

    def load_config_raw(self) -> str:
        """Read config.yml text."""
        self.ensure_active_config_exists()
        return self.config_path.read_text(encoding="utf-8")

    def load_config_dict(self) -> Dict[str, Any]:
        """Parse config.yml into Python dictionary."""
        raw = self.load_config_raw()
        return yaml.safe_load(raw) or {}

    def save_config_dict(self, data: Dict[str, Any], summary: str = "Update config.yml"):
        """Serialize dictionary back to config.yml with backup."""
        self.ensure_active_config_exists()
        backup_manager.create_backup(summary=summary, changed_files=["config.yml"])
        # Enforce DVMHost systemd and execution requirements
        data["iAgreeNotToBeStupid"] = True
        data["daemon"] = False
        content = yaml.dump(data, default_flow_style=False, sort_keys=False)
        backup_manager.atomic_write(self.config_path, content)

    def get_active_protocol(self) -> str:
        """Detect whether DMR or P25 is active."""
        conf = self.load_config_dict()
        dmr_on = conf.get("protocols", {}).get("dmr", {}).get("enable", False)
        p25_on = conf.get("protocols", {}).get("p25", {}).get("enable", False)
        if dmr_on and not p25_on:
            return "dmr"
        if p25_on and not dmr_on:
            return "p25"
        if dmr_on and p25_on:
            return "multimode"
        return "none"

    def get_quick_status(self) -> Dict[str, Any]:
        """Extract high-level overview info for dashboard."""
        conf = self.load_config_dict()
        sys_conf = conf.get("system", {}).get("config", {})
        modem_conf = conf.get("system", {}).get("modem", {})
        proto_conf = conf.get("protocols", {})

        protocol = self.get_active_protocol()
        is_duplex = conf.get("system", {}).get("duplex", True)

        modem_proto = modem_conf.get("protocol", {})
        modem_port = modem_proto.get("uart", {}).get("port", "/dev/ttyAMA0")
        modem_speed = modem_proto.get("uart", {}).get("speed", 115200)

        # Frequencies
        rx_freq_mhz = None
        tx_freq_mhz = None
        channel_id = sys_conf.get("channelId", 0)
        channel_no = sys_conf.get("channelNo", 1)

        # DMR and P25 specifics
        color_code = sys_conf.get("colorCode", 1)
        nac = sys_conf.get("nac", "293")
        net_id = sys_conf.get("netId", "BB800")
        callsign = conf.get("system", {}).get("identity", "NEXUS")

        return {
            "protocol": protocol,
            "callsign": callsign,
            "duplex": is_duplex,
            "channel_id": channel_id,
            "channel_no": channel_no,
            "color_code": color_code,
            "nac": nac,
            "net_id": net_id,
            "modem_port": modem_port,
            "modem_speed": modem_speed,
            "dmr_enabled": proto_conf.get("dmr", {}).get("enable", False),
            "p25_enabled": proto_conf.get("p25", {}).get("enable", False),
            "network_enabled": conf.get("network", {}).get("enable", False),
            "network_address": conf.get("network", {}).get("address", "127.0.0.1"),
            "network_port": conf.get("network", {}).get("port", 62031),
            "network_peer_id": conf.get("network", {}).get("id", 100000),
            "rest_enabled": conf.get("network", {}).get("restEnable", True),
            "rest_port": conf.get("network", {}).get("restPort", 9990),
        }

    # Talkgroups management
    def load_talkgroup_rules(self) -> List[Dict[str, Any]]:
        """Load and parse talkgroup_rules.yml."""
        self.ensure_active_config_exists()
        if not self.tg_rules_path.exists():
            return []
        try:
            data = yaml.safe_load(self.tg_rules_path.read_text(encoding="utf-8"))
            rules = []
            if isinstance(data, dict):
                # Format can be {rules: [...]} or list
                items = data.get("rules", []) or data.get("talkgroups", [])
                if isinstance(items, list):
                    rules = items
            elif isinstance(data, list):
                rules = data
            return rules
        except Exception:
            return []

    def save_talkgroup_rules(self, rules: List[Dict[str, Any]], summary: str = "Update talkgroups"):
        """Save talkgroup rules to talkgroup_rules.yml."""
        self.ensure_active_config_exists()
        backup_manager.create_backup(summary=summary, changed_files=["talkgroup_rules.yml"])
        content = yaml.dump({"rules": rules}, default_flow_style=False, sort_keys=False)
        backup_manager.atomic_write(self.tg_rules_path, content)

    # Radio ID ACL management
    def load_rid_acl(self) -> List[Dict[str, Any]]:
        """Load and parse rid_acl.dat CSV."""
        self.ensure_active_config_exists()
        if not self.rid_acl_path.exists():
            return []
        rids = []
        for line in self.rid_acl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 2:
                try:
                    rids.append({
                        "rid": int(parts[0]),
                        "enabled": bool(int(parts[1])),
                        "alias": parts[2] if len(parts) > 2 else "",
                        "ip": parts[3] if len(parts) > 3 else "",
                    })
                except ValueError:
                    continue
        return rids

    def save_rid_acl(self, rids: List[Dict[str, Any]], summary: str = "Update Radio ID ACL"):
        """Save Radio ID list to rid_acl.dat."""
        self.ensure_active_config_exists()
        backup_manager.create_backup(summary=summary, changed_files=["rid_acl.dat"])
        lines = [
            "#",
            "# Digital Voice Modem - Radio ID Access Control List",
            "# Format: RID,Enabled (1/0),Alias,IP Address,",
            "#",
        ]
        for item in rids:
            en = 1 if item.get("enabled", True) else 0
            lines.append(f"{item.get('rid')},{en},{item.get('alias', '')},{item.get('ip', '')},")
        lines.append("")
        backup_manager.atomic_write(self.rid_acl_path, "\n".join(lines))


dvm_config_manager = DVMConfigManager()
