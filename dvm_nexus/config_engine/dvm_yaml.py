"""
DVM Nexus - DVMHost YAML & Data Table Manager
Provides structured access, modifications, and validation for DVMHost files.
"""

import os
import csv
import io
import subprocess
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dvm_nexus.core.config import settings, DEFAULT_CONFIGS_DIR, PROFILES_DIR
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.database import log_audit


class IndentedSafeDumper(yaml.SafeDumper):
    """
    Custom YAML dumper that indents sequences under mapping keys.
    Required for C++ yaml-cpp compatibility in DVMHost.
    """
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow=flow, indentless=False)


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

        # Safety block: Self-heal active configuration if legacy multi-protocol collision is present
        try:
            if self.config_path.exists():
                raw = self.config_path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw)
                if isinstance(data, dict):
                    protocols = data.get("protocols", {})
                    if isinstance(protocols, dict):
                        dmr_on = bool(protocols.get("dmr", {}).get("enable", False)) if isinstance(protocols.get("dmr"), dict) else False
                        p25_on = bool(protocols.get("p25", {}).get("enable", False)) if isinstance(protocols.get("p25"), dict) else False
                        nxdn_on = bool(protocols.get("nxdn", {}).get("enable", False)) if isinstance(protocols.get("nxdn"), dict) else False
                        if sum([dmr_on, p25_on, nxdn_on]) > 1:
                            protocols.setdefault("p25", {})["enable"] = False
                            if "nxdn" in protocols and isinstance(protocols["nxdn"], dict):
                                protocols["nxdn"]["enable"] = False
                            content = yaml.dump(data, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False)
                            backup_manager.atomic_write(self.config_path, content)
                            log_audit("SAFETY", "AUTO_HEAL_ACTIVE_CONFIG", "Self-healed multi-protocol collision in active config.yml")
        except Exception:
            pass

        # Fix directory and file permissions on Linux so dvmhost can read and write logs
        if os.name != "nt":
            try:
                subprocess.run(["chmod", "-R", "a+rwX", str(self.dvm_dir)], capture_output=True)
                import pwd
                try:
                    pwd.getpwnam("dvmhost")
                    subprocess.run(["chown", "-R", "dvmhost:dvmhost", str(self.dvm_dir)], capture_output=True)
                except KeyError:
                    pass
            except Exception:
                pass

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

        # Configure logging for systemd journald and prevent file open panics
        if "log" not in data or not isinstance(data["log"], dict):
            data["log"] = {}
        data["log"]["useSyslog"] = True
        data["log"]["filePath"] = "/opt/dvm" if os.name != "nt" else str(self.dvm_dir)
        data["log"]["activityFilePath"] = "/opt/dvm" if os.name != "nt" else str(self.dvm_dir)
        data["log"]["fileRoot"] = data["log"].get("fileRoot") or "DVM"

        # Ensure DVMHost REST API is enabled for over-the-air commands and live query
        if "network" not in data or not isinstance(data["network"], dict):
            data["network"] = {}
        data["network"]["restEnable"] = True
        if "restPort" not in data["network"]:
            data["network"]["restPort"] = 9990
        if "restAddress" not in data["network"]:
            data["network"]["restAddress"] = "127.0.0.1"
        if "restPassword" not in data["network"]:
            data["network"]["restPassword"] = "PASSWORD"

        # Safety block: Enforce single-protocol mutual exclusion for hotspot / UART modems.
        # ADF7021 MMDVM_HS firmware aborts with NAK 32 (HS_NO_DUAL_MODE) if multiple protocols are enabled.
        protocols = data.get("protocols", {})
        if isinstance(protocols, dict):
            dmr_on = bool(protocols.get("dmr", {}).get("enable", False)) if isinstance(protocols.get("dmr"), dict) else False
            p25_on = bool(protocols.get("p25", {}).get("enable", False)) if isinstance(protocols.get("p25"), dict) else False
            nxdn_on = bool(protocols.get("nxdn", {}).get("enable", False)) if isinstance(protocols.get("nxdn"), dict) else False

            if sum([dmr_on, p25_on, nxdn_on]) > 1:
                if "p25" in summary.lower() or (p25_on and not dmr_on):
                    protocols.setdefault("dmr", {})["enable"] = False
                    if "nxdn" in protocols and isinstance(protocols["nxdn"], dict):
                        protocols["nxdn"]["enable"] = False
                else:
                    protocols.setdefault("p25", {})["enable"] = False
                    if "nxdn" in protocols and isinstance(protocols["nxdn"], dict):
                        protocols["nxdn"]["enable"] = False
                log_audit(
                    "SAFETY",
                    "PROTOCOL_MUTUAL_EXCLUSION",
                    "Enforced single-protocol mode in config.yml to prevent modem NAK 32 (HS_NO_DUAL_MODE)"
                )

        content = yaml.dump(data, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False)
        backup_manager.atomic_write(self.config_path, content)

        if os.name != "nt":
            try:
                subprocess.run(["chmod", "-R", "a+rwX", str(self.dvm_dir)], capture_output=True)
                import pwd
                try:
                    pwd.getpwnam("dvmhost")
                    subprocess.run(["chown", "-R", "dvmhost:dvmhost", str(self.dvm_dir)], capture_output=True)
                except KeyError:
                    pass
            except Exception:
                pass

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

    def get_tuned_frequencies(self) -> Tuple[float, float]:
        """
        Calculate active tuned TX and RX frequencies in MHz from channelId/channelNo and iden_table.dat.
        """
        conf = self.load_config_dict()
        sys_conf = conf.get("system", {}).get("config", {})
        channel_id = sys_conf.get("channelId", 2)
        channel_no = sys_conf.get("channelNo", 1)
        from dvm_nexus.config_engine.iden_calculator import read_iden_table_file, calculate_frequency_from_channel
        iden_entries = read_iden_table_file(self.iden_path)
        for entry in iden_entries:
            if entry.channel_id == channel_id:
                tx_hz, rx_hz = calculate_frequency_from_channel(
                    channel_no=channel_no,
                    base_freq_hz=entry.base_freq_hz,
                    spacing_khz=entry.spacing_khz,
                    offset_mhz=entry.offset_mhz
                )
                return round(tx_hz / 1000000.0, 6), round(rx_hz / 1000000.0, 6)
        return 462.61875, 467.61875

    def set_tuned_frequencies(self, tx_mhz: float, rx_mhz: float, conf_dict: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
        """
        Calculate IDEN entry for target frequencies, update iden_table.dat, 
        and update channelId / channelNo in config dict.
        Returns: (channel_id, channel_no)
        """
        from dvm_nexus.config_engine.iden_calculator import (
            read_iden_table_file, format_iden_table, find_or_create_iden_entry
        )
        tx_hz = int(round(tx_mhz * 1000000.0))
        rx_hz = int(round(rx_mhz * 1000000.0))
        iden_entries = read_iden_table_file(self.iden_path)
        entry, channel_no = find_or_create_iden_entry(iden_entries, tx_hz, rx_hz)
        backup_manager.atomic_write(self.iden_path, format_iden_table(iden_entries))

        if conf_dict is not None:
            sys_conf = conf_dict.setdefault("system", {}).setdefault("config", {})
            sys_conf["channelId"] = entry.channel_id
            sys_conf["channelNo"] = channel_no
            if "voiceChNo" in sys_conf and isinstance(sys_conf["voiceChNo"], list) and len(sys_conf["voiceChNo"]) > 0:
                sys_conf["voiceChNo"][0]["channelId"] = entry.channel_id
                sys_conf["voiceChNo"][0]["channelNo"] = channel_no
                is_duplex = conf_dict.get("system", {}).get("duplex", True)
                if not is_duplex:
                    sys_conf["voiceChNo"][0].pop("rxChannelId", None)
                    sys_conf["voiceChNo"][0].pop("rxChannelNo", None)
        return entry.channel_id, channel_no

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
        tx_freq_mhz, rx_freq_mhz = self.get_tuned_frequencies()
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
            "tx_freq_mhz": tx_freq_mhz,
            "rx_freq_mhz": rx_freq_mhz,
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
        """Load and parse talkgroup_rules.yml (supporting official groupVoice and legacy rules formats)."""
        self.ensure_active_config_exists()
        if not self.tg_rules_path.exists():
            return []
        try:
            data = yaml.safe_load(self.tg_rules_path.read_text(encoding="utf-8"))
            rules = []
            if isinstance(data, dict):
                if "groupVoice" in data and isinstance(data["groupVoice"], list):
                    for item in data["groupVoice"]:
                        if not isinstance(item, dict):
                            continue
                        src = item.get("source", {})
                        tgid = src.get("tgid") or item.get("id") or item.get("tgid")
                        if tgid is None:
                            continue
                        name = item.get("name") or f"Talkgroup {tgid}"
                        slot = src.get("slot") or item.get("slot") or 1
                        cfg = item.get("config", {})
                        parrot = bool(cfg.get("parrot", False))
                        active = bool(cfg.get("active", True))
                        rules.append({
                            "id": int(tgid),
                            "tgid": int(tgid),
                            "name": str(name),
                            "slot": int(slot),
                            "parrot": parrot,
                            "active": active,
                        })
                    return rules
                items = data.get("rules", []) or data.get("talkgroups", [])
                if isinstance(items, list):
                    for r in items:
                        if isinstance(r, dict):
                            tgid = r.get("id") or r.get("tgid", 1)
                            rules.append({
                                "id": int(tgid),
                                "tgid": int(tgid),
                                "name": str(r.get("name", f"Talkgroup {tgid}")),
                                "slot": int(r.get("slot", 1)),
                                "parrot": bool(r.get("parrot", False)),
                                "active": bool(r.get("active", True)),
                            })
                    return rules
            elif isinstance(data, list):
                for r in data:
                    if isinstance(r, dict):
                        tgid = r.get("id") or r.get("tgid", 1)
                        rules.append({
                            "id": int(tgid),
                            "tgid": int(tgid),
                            "name": str(r.get("name", f"Talkgroup {tgid}")),
                            "slot": int(r.get("slot", 1)),
                            "parrot": bool(r.get("parrot", False)),
                            "active": bool(r.get("active", True)),
                        })
            return rules
        except Exception:
            return []

    def save_talkgroup_rules(self, rules: List[Dict[str, Any]], summary: str = "Update talkgroups"):
        """Save talkgroup rules to talkgroup_rules.yml in official DVMHost groupVoice format."""
        self.ensure_active_config_exists()
        backup_manager.create_backup(summary=summary, changed_files=["talkgroup_rules.yml"])
        
        gv_list = []
        for r in rules:
            tgid = int(r.get("id") or r.get("tgid", 1))
            gv_list.append({
                "name": str(r.get("name", f"Talkgroup {tgid}")),
                "config": {
                    "active": bool(r.get("active", True)),
                    "affiliated": False,
                    "parrot": bool(r.get("parrot", False)),
                    "inclusion": [],
                    "exclusion": [],
                    "rewrite": [],
                    "always": [],
                    "preferred": [],
                    "rid_permitted": [],
                },
                "source": {
                    "tgid": tgid,
                    "slot": int(r.get("slot", 1)),
                }
            })
        
        content = yaml.dump({"groupVoice": gv_list}, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False)
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
