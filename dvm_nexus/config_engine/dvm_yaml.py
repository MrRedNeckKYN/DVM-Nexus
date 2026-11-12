"""
DVM Nexus - DVMHost YAML & Data Table Manager
Provides structured access, modifications, and validation for DVMHost files.
"""

import os
import csv
import io
import logging
import subprocess
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dvm_nexus.core.config import settings, DEFAULT_CONFIGS_DIR, PROFILES_DIR
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.database import log_audit

logger = logging.getLogger(__name__)


class IndentedSafeDumper(yaml.SafeDumper):
    """
    Custom YAML dumper that indents sequences under mapping keys.
    Required for C++ yaml-cpp compatibility in DVMHost.
    """
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow=flow, indentless=False)


def is_banned_p25_wacn(val: Any) -> bool:
    """
    Check if a P25 WACN (netId) falls in the 0xBEE00..0xBEEFF range.
    DVMHost upstream (src/host/Host.Config.cpp:350 & src/host/Host.cpp:649)
    unconditionally calls ::fatal("error 4") on 0xBEE00 and ::fatal("... error 38258")
    on any netId where (netId >> 8) == 0xBEE — even when running in DMR mode!
    """
    if val is None:
        return False
    s = str(val).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    elif s.startswith("$"):
        s = s[1:]
    try:
        int_val = int(s, 16)
        return (int_val >> 8) == 0xBEE
    except ValueError:
        return False


def sanitize_p25_wacn(val: Any) -> str:
    """
    Return a safe, valid 20-bit hexadecimal P25 WACN (netId).
    Replaces empty, invalid, 0, or banned 0xBEE00..0xBEEFF values with 'BB800'.
    """
    if val is None:
        return "BB800"
    s = str(val).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    elif s.startswith("$"):
        s = s[1:]
    if not s:
        return "BB800"
    try:
        int_val = int(s, 16)
    except ValueError:
        return "BB800"
    if int_val <= 0 or int_val > 0xFFFFF or (int_val >> 8) == 0xBEE:
        return "BB800"
    return f"{int_val:X}"


def normalize_p25_nac(val: Any) -> str:
    """
    Normalize a P25 NAC (12-bit hex, e.g. '293', '0x293', '$293') into an uppercase
    hex string without prefix (e.g. '293') for DVMHost ::strtoul(..., NULL, 16).
    """
    s = str(val if val is not None else "293").strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    elif s.startswith("$"):
        s = s[1:]
    try:
        int_val = int(s, 16)
        if 1 <= int_val <= 0xFFF:
            return f"{int_val:03X}"
    except ValueError:
        pass
    return "293"


def normalize_p25_sys_id(val: Any) -> str:
    """
    Normalize a P25 System ID (12-bit hex, e.g. '001', '0x001', '$001') into an uppercase
    3-digit hex string (e.g. '001') for DVMHost ::strtoul(..., NULL, 16).
    """
    s = str(val if val is not None else "001").strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    elif s.startswith("$"):
        s = s[1:]
    try:
        int_val = int(s, 16)
        if 1 <= int_val <= 0xFFF:
            return f"{int_val:03X}"
    except ValueError:
        pass
    return "001"



def parse_channel_no(val: Any) -> int:
    """
    Parse channelNo from config.yml into a decimal integer (1..4095).
    DVMHost reads channelNo in base-16 (::strtoul(..., NULL, 16)).
    - If val is an unquoted Python int (legacy decimal format written by older Nexus),
      interpret it directly as decimal (e.g., 2019 -> 2019).
    - If val is a string (e.g., '7E3', '1', '100'), interpret it as base-16 hex
      (with fallback to base-10 if int(s, 16) > 4095 and s.isdigit()).
    """
    if isinstance(val, int) and not isinstance(val, bool):
        return max(1, min(4095, val))
    s = str(val or "1").strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    elif s.startswith("$"):
        s = s[1:]
    if not s:
        return 1
    try:
        hex_val = int(s, 16)
        if 1 <= hex_val <= 4095:
            return hex_val
        if hex_val > 4095 and s.isdigit():
            dec_val = int(s, 10)
            if 1 <= dec_val <= 4095:
                return dec_val
        return max(1, min(4095, hex_val))
    except ValueError:
        try:
            return max(1, min(4095, int(s, 10)))
        except ValueError:
            return 1


def format_channel_no(ch_no: int) -> str:
    """
    Format a channel number (1..4095) as an uppercase hexadecimal string for DVMHost.
    DVMHost parses system.config.channelNo and voiceChNo[i].channelNo with
    ::strtoul(..., NULL, 16), so storing a hex string ensures exact frequency tuning.
    """
    clamped = max(1, min(4095, int(ch_no)))
    return f"{clamped:X}"


class DVMConfigManager:
    """Manages active DVMHost configuration files."""

    _permissions_fixed = False
    _profiles_healed = False

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

    def _heal_yaml_dict(self, data: Dict[str, Any], include_policy: bool = False) -> bool:
        """
        Inspect a config.yml dict and heal any fatal DVMHost configuration traps in-place:
        1. P25 WACN (netId) in 0xBEE00..0xBEEFF -> 'BB800' (prevents FATAL PANIC; error 4 / error 38258)
        2. Decimal integer channelNo -> uppercase hex string (prevents $0FFF clamping in strtoul base-16)
        3. Multi-protocol collision -> single active protocol (prevents MMDVM NAK 32)
        4. Voice channel RPC port collision -> 0
        5. Cross-platform log.filePath on Linux -> '/opt/dvm'
        When include_policy=True, also enforces iAgreeNotToBeStupid=True, daemon=False, and protocol.type='uart'.
        Returns True if any field was modified.
        """
        modified = False
        if not isinstance(data, dict):
            return False

        if include_policy:
            if data.get("iAgreeNotToBeStupid") is not True:
                data["iAgreeNotToBeStupid"] = True
                modified = True
            if data.get("daemon") is not False:
                data["daemon"] = False
                modified = True

        # Heal log path on Linux if it points to a Windows drive path
        log_blk = data.get("log")
        if isinstance(log_blk, dict) and os.name != "nt":
            for k in ("filePath", "activityFilePath"):
                v = str(log_blk.get(k, "")).strip()
                if ":" in v or "\\" in v:
                    log_blk[k] = "/opt/dvm"
                    modified = True

        # Heal modem 'null' trap when include_policy is True
        if include_policy:
            modem_blk = data.get("system", {}).get("modem", {}) if isinstance(data.get("system"), dict) else {}
            if isinstance(modem_blk, dict):
                proto_blk = modem_blk.get("protocol", {})
                if isinstance(proto_blk, dict):
                    curr_type = str(proto_blk.get("type", "")).strip().lower()
                    if curr_type in ("null", "'null'", '"null"', ""):
                        proto_blk["type"] = "uart"
                        modified = True

        # Heal system.config: netId (BEE00 panic), nac, sysId, channelNo (base-16 hex), and voiceChNo
        sys_config = data.get("system", {}).get("config", {}) if isinstance(data.get("system"), dict) else {}
        if isinstance(sys_config, dict):
            raw_net_id = sys_config.get("netId", "BB800")
            safe_net_id = sanitize_p25_wacn(raw_net_id)
            if str(raw_net_id) != safe_net_id:
                sys_config["netId"] = safe_net_id
                modified = True

            raw_nac = sys_config.get("nac", "293")
            safe_nac = normalize_p25_nac(raw_nac)
            if str(raw_nac) != safe_nac:
                sys_config["nac"] = safe_nac
                modified = True

            raw_sys_id = sys_config.get("sysId", "001")
            safe_sys_id = normalize_p25_sys_id(raw_sys_id)
            if str(raw_sys_id) != safe_sys_id:
                sys_config["sysId"] = safe_sys_id
                modified = True

            raw_ch_no = sys_config.get("channelNo", "1")
            hex_ch_no = format_channel_no(parse_channel_no(raw_ch_no))
            if not isinstance(raw_ch_no, str) or raw_ch_no != hex_ch_no:
                sys_config["channelNo"] = hex_ch_no
                modified = True

            root_rpc_port = data.get("network", {}).get("rpcPort", 9890) if isinstance(data.get("network"), dict) else 9890
            if "voiceChNo" in sys_config and isinstance(sys_config["voiceChNo"], list):
                for vc in sys_config["voiceChNo"]:
                    if isinstance(vc, dict):
                        vc_ch = vc.get("channelNo", hex_ch_no)
                        vc_hex = format_channel_no(parse_channel_no(vc_ch))
                        if not isinstance(vc_ch, str) or vc_ch != vc_hex:
                            vc["channelNo"] = vc_hex
                            modified = True
                        if "rxChannelNo" in vc:
                            rx_ch = vc.get("rxChannelNo")
                            rx_hex = format_channel_no(parse_channel_no(rx_ch))
                            if not isinstance(rx_ch, str) or rx_ch != rx_hex:
                                vc["rxChannelNo"] = rx_hex
                                modified = True
                        if vc.get("rpcPort") == root_rpc_port and root_rpc_port != 0:
                            vc["rpcPort"] = 0
                            modified = True

        # Ensure radio_id and talkgroup_id lookup blocks have allowUnknown / acl defaults
        sys_blk = data.get("system")
        if isinstance(sys_blk, dict):
            rid_blk = sys_blk.setdefault("radio_id", {"file": "rid_acl.dat", "time": 2, "acl": False})
            if isinstance(rid_blk, dict):
                acl_en = bool(rid_blk.get("acl", False))
                if rid_blk.get("allowUnknown") != (not acl_en):
                    rid_blk["allowUnknown"] = not acl_en
                    modified = True
            tg_blk = sys_blk.setdefault("talkgroup_id", {"file": "talkgroup_rules.yml", "time": 2, "acl": False})
            if isinstance(tg_blk, dict) and "acl" not in tg_blk:
                tg_blk["acl"] = False
                modified = True

        # Heal multi-protocol collision & mirror colorCode / P25 IDs
        protocols = data.get("protocols", {})
        if isinstance(protocols, dict):
            dmr_blk = protocols.get("dmr")
            if isinstance(dmr_blk, dict) and isinstance(sys_config, dict):
                cc_val = int(sys_config.get("colorCode", dmr_blk.get("colorCode", 1)))
                if dmr_blk.get("colorCode") != cc_val:
                    dmr_blk["colorCode"] = cc_val
                    modified = True
            p25_blk = protocols.get("p25")
            if isinstance(p25_blk, dict) and isinstance(sys_config, dict):
                for k, v in (
                    ("nac", sys_config.get("nac", "293")),
                    ("sysId", sys_config.get("sysId", "001")),
                    ("wacn", sys_config.get("netId", "BB800")),
                    ("netId", sys_config.get("netId", "BB800")),
                ):
                    if p25_blk.get(k) != v:
                        p25_blk[k] = v
                        modified = True
            dmr_on = bool(protocols.get("dmr", {}).get("enable", False)) if isinstance(protocols.get("dmr"), dict) else False
            p25_on = bool(protocols.get("p25", {}).get("enable", False)) if isinstance(protocols.get("p25"), dict) else False
            nxdn_on = bool(protocols.get("nxdn", {}).get("enable", False)) if isinstance(protocols.get("nxdn"), dict) else False
            if sum([dmr_on, p25_on, nxdn_on]) > 1:
                protocols.setdefault("p25", {})["enable"] = False
                if "nxdn" in protocols and isinstance(protocols["nxdn"], dict):
                    protocols["nxdn"]["enable"] = False
                modified = True

        return modified

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

        # Safety block: Self-heal active configuration if legacy traps (BEE00, decimal channelNo, multi-protocol) are present
        try:
            if self.config_path.exists():
                raw = self.config_path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw)
                if isinstance(data, dict) and self._heal_yaml_dict(data):
                    content = yaml.dump(data, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False)
                    backup_manager.atomic_write(self.config_path, content)
                    log_audit("SAFETY", "AUTO_HEAL_ACTIVE_CONFIG", "Self-healed active config.yml (WACN/channelNo/protocol safety)")
        except Exception:
            logger.exception("Could not self-heal active config.yml")

        # One-time per process: Self-heal saved profile config.yml files so switching profiles never introduces a trap
        if not DVMConfigManager._profiles_healed:
            DVMConfigManager._profiles_healed = True
            try:
                if PROFILES_DIR.exists():
                    for prof_cfg in PROFILES_DIR.glob("*/config.yml"):
                        try:
                            p_raw = prof_cfg.read_text(encoding="utf-8")
                            p_data = yaml.safe_load(p_raw)
                            if isinstance(p_data, dict) and self._heal_yaml_dict(p_data, include_policy=True):
                                p_content = yaml.dump(p_data, Dumper=IndentedSafeDumper, default_flow_style=False, sort_keys=False)
                                backup_manager.atomic_write(prof_cfg, p_content)
                        except Exception:
                            logger.exception("Could not self-heal profile %s", prof_cfg)
            except Exception:
                pass

        self._ensure_permissions_once()

    def _ensure_permissions_once(self):
        """
        One-time (per process) permission setup for the DVMHost directory on Linux.

        Previously this ran `chmod -R a+rwX` + `chown -R` on EVERY config read, which forked
        processes constantly and left /opt/dvm (REST/network passwords) world-writable.
        Files are now owner/group accessible only; atomic_write() preserves mode/owner afterwards.
        """
        if os.name == "nt" or DVMConfigManager._permissions_fixed:
            return
        DVMConfigManager._permissions_fixed = True
        try:
            subprocess.run(["chmod", "-R", "u+rwX,g+rwX,o-rwx", str(self.dvm_dir)], capture_output=True, timeout=15)
            import pwd
            try:
                pwd.getpwnam("dvmhost")
                subprocess.run(["chown", "-R", "dvmhost:dvmhost", str(self.dvm_dir)], capture_output=True, timeout=15)
            except KeyError:
                pass
        except Exception:
            logger.exception("Could not normalise permissions on %s", self.dvm_dir)

    def get_config_revision(self) -> str:
        """Short content hash of config.yml; used to detect stale forms (edited elsewhere)."""
        import hashlib
        try:
            return hashlib.sha256(self.config_path.read_bytes()).hexdigest()[:16]
        except Exception:
            return ""

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

        # Hardware Safety Net: Never allow 'null' modem controller when operating hardware
        if "system" not in data or not isinstance(data["system"], dict):
            data["system"] = {}
        if "modem" not in data["system"] or not isinstance(data["system"]["modem"], dict):
            data["system"]["modem"] = {}
        modem_blk = data["system"]["modem"]
        if "protocol" not in modem_blk or not isinstance(modem_blk["protocol"], dict):
            modem_blk["protocol"] = {}

        # Enforce hardware UART mode (reject dummy null simulator)
        curr_type = str(modem_blk["protocol"].get("type", "")).strip().lower()
        if curr_type in ("null", "'null'", '"null"', ""):
            modem_blk["protocol"]["type"] = "uart"
            log_audit("SAFETY", "MODEM_FIX", "Enforced protocol.type = 'uart' (prevented dummy null simulator)")

        if not modem_blk["protocol"].get("mode"):
            modem_blk["protocol"]["mode"] = "air"

        if "uart" not in modem_blk["protocol"] or not isinstance(modem_blk["protocol"]["uart"], dict):
            modem_blk["protocol"]["uart"] = {}

        # Default port to /dev/ttyAMA0 if null or empty or on Linux with ttyUSB0 missing
        curr_port = str(modem_blk["protocol"]["uart"].get("port", "")).strip()
        if not curr_port or curr_port == "null":
            modem_blk["protocol"]["uart"]["port"] = "/dev/ttyAMA0"
        elif curr_port == "/dev/ttyUSB0" and os.name != "nt" and not os.path.exists("/dev/ttyUSB0"):
            # If ttyUSB0 does not physically exist but GPIO ttyAMA0 does, auto-route to GPIO
            if os.path.exists("/dev/ttyAMA0") or os.path.exists("/dev/serial0"):
                target_port = "/dev/ttyAMA0" if os.path.exists("/dev/ttyAMA0") else "/dev/serial0"
                modem_blk["protocol"]["uart"]["port"] = target_port
                log_audit("SAFETY", "UART_FALLBACK", f"Redirected missing /dev/ttyUSB0 to hardware GPIO {target_port}")

        if not modem_blk["protocol"]["uart"].get("speed"):
            modem_blk["protocol"]["uart"]["speed"] = 115200

        # Safety Net: Authoritative Standalone Mode and Voice Channel RPC conflict prevention
        # Prevents: "W: (HOST) Detected possible misconfiguration! Voice Channel RPC Address/Port and the configured RPC Address/Port are the same!"
        # Prevents: "W: (HOST) Host is non-authoritative! This requires a second instance configured in site controller mode."
        sys_config = data.get("system", {}).get("config", {})
        if isinstance(sys_config, dict):
            # Enforce authoritative master mode unless explicitly configured otherwise
            if "authoritative" not in sys_config:
                sys_config["authoritative"] = True

            # Safety Net: Prevent DVMHost FATAL PANIC error 4 / error 38258 on 0xBEE00..0xBEEFF WACN
            raw_net_id = sys_config.get("netId", "BB800")
            safe_net_id = sanitize_p25_wacn(raw_net_id)
            if str(raw_net_id) != safe_net_id:
                sys_config["netId"] = safe_net_id
                log_audit("SAFETY", "WACN_PANIC_FIX", f"Replaced banned/invalid P25 WACN '{raw_net_id}' with '{safe_net_id}' to prevent DVMHost FATAL PANIC error 4")

            sys_config["nac"] = normalize_p25_nac(sys_config.get("nac", "293"))
            sys_config["sysId"] = normalize_p25_sys_id(sys_config.get("sysId", "001"))

            # Safety Net: Ensure channelNo is formatted in base-16 hex for DVMHost ::strtoul(..., NULL, 16)
            hex_ch_no = format_channel_no(parse_channel_no(sys_config.get("channelNo", "1")))
            sys_config["channelNo"] = hex_ch_no

            # If voiceChNo exists in standalone mode, its rpcPort must not collide with system.rpcPort
            root_rpc_port = data.get("network", {}).get("rpcPort", 9890)
            if "voiceChNo" in sys_config and isinstance(sys_config["voiceChNo"], list):
                for vc in sys_config["voiceChNo"]:
                    if isinstance(vc, dict):
                        vc["channelNo"] = format_channel_no(parse_channel_no(vc.get("channelNo", hex_ch_no)))
                        if "rxChannelNo" in vc:
                            vc["rxChannelNo"] = format_channel_no(parse_channel_no(vc["rxChannelNo"]))
                        vc_rpc_port = vc.get("rpcPort")
                        if vc_rpc_port == root_rpc_port:
                            vc["rpcPort"] = 0
                            log_audit("SAFETY", "VC_RPC_COLLISION_FIX", f"Set voiceChNo rpcPort to 0 to prevent RPC port collision with main daemon (port {root_rpc_port})")

        # Ensure radio_id and talkgroup_id lookup blocks allow unknown RIDs/TGs unless ACL is explicitly enabled
        rid_blk = data["system"].setdefault("radio_id", {"file": "rid_acl.dat", "time": 2, "acl": False})
        if isinstance(rid_blk, dict):
            rid_blk.setdefault("file", "rid_acl.dat")
            rid_blk.setdefault("time", 2)
            acl_on = bool(rid_blk.get("acl", False))
            rid_blk["acl"] = acl_on
            rid_blk["allowUnknown"] = not acl_on

        tg_blk = data["system"].setdefault("talkgroup_id", {"file": "talkgroup_rules.yml", "time": 2, "acl": False})
        if isinstance(tg_blk, dict):
            tg_blk.setdefault("file", "talkgroup_rules.yml")
            tg_blk.setdefault("time", 2)
            tg_blk["acl"] = bool(tg_blk.get("acl", False))

        # Safety block: Enforce single-protocol mutual exclusion for hotspot / UART modems.
        # ADF7021 MMDVM_HS firmware aborts with NAK 32 (HS_NO_DUAL_MODE) if multiple protocols are enabled.
        protocols = data.get("protocols", {})
        if isinstance(protocols, dict):
            if isinstance(protocols.get("dmr"), dict) and isinstance(sys_config, dict):
                protocols["dmr"]["colorCode"] = int(sys_config.get("colorCode", protocols["dmr"].get("colorCode", 1)))
            if isinstance(protocols.get("p25"), dict) and isinstance(sys_config, dict):
                protocols["p25"]["nac"] = sys_config.get("nac", "293")
                protocols["p25"]["sysId"] = sys_config.get("sysId", "001")
                protocols["p25"]["wacn"] = sys_config.get("netId", "BB800")
                protocols["p25"]["netId"] = sys_config.get("netId", "BB800")
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
        # (permissions/ownership are preserved by atomic_write; no recursive chmod per save)

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
        is_duplex = bool(conf.get("system", {}).get("duplex", True))
        simplex_same = bool(conf.get("system", {}).get("simplexSameFrequency", not is_duplex))
        channel_id = int(sys_conf.get("channelId", 1))
        channel_no = parse_channel_no(sys_conf.get("channelNo", "1"))
        from dvm_nexus.config_engine.iden_calculator import read_iden_table_file, calculate_frequency_from_channel
        iden_entries = read_iden_table_file(self.iden_path)
        for entry in iden_entries:
            if entry.channel_id == channel_id:
                tx_hz, rx_hz = calculate_frequency_from_channel(
                    channel_no=channel_no,
                    base_freq_hz=entry.base_freq_hz,
                    spacing_khz=entry.spacing_khz,
                    offset_mhz=entry.offset_mhz,
                    duplex=is_duplex,
                    simplex_same_freq=simplex_same,
                )
                return round(tx_hz / 1000000.0, 6), round(rx_hz / 1000000.0, 6)
        return 462.61875, (467.61875 if is_duplex else 462.61875)

    def get_rf_verification(self, conf: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Return the exact DVMHost calculated RX/TX frequencies in Hz, effective frequencies with
        calibration offsets (rxTuning/txTuning), and auto-calculated iden_table.dat parameters.
        """
        c = conf if conf is not None else self.load_config_dict()
        sys_blk = c.get("system", {})
        sys_conf = sys_blk.get("config", {})
        hotspot = sys_blk.get("modem", {}).get("hotspot", {})
        is_duplex = bool(sys_blk.get("duplex", True))
        simplex_same = bool(sys_blk.get("simplexSameFrequency", not is_duplex))
        channel_id = int(sys_conf.get("channelId", 1))
        channel_no = parse_channel_no(sys_conf.get("channelNo", "1"))
        rx_tuning_hz = int(hotspot.get("rxTuning", 0) or 0)
        tx_tuning_hz = int(hotspot.get("txTuning", 0) or 0)

        from dvm_nexus.config_engine.iden_calculator import read_iden_table_file, calculate_frequency_from_channel
        iden_entries = read_iden_table_file(self.iden_path)
        chosen = None
        for entry in iden_entries:
            if entry.channel_id == channel_id:
                chosen = entry
                break
        if chosen is None and iden_entries:
            chosen = iden_entries[0]

        if chosen is not None:
            tx_hz, rx_hz = calculate_frequency_from_channel(
                channel_no=channel_no,
                base_freq_hz=chosen.base_freq_hz,
                spacing_khz=chosen.spacing_khz,
                offset_mhz=chosen.offset_mhz,
                duplex=is_duplex,
                simplex_same_freq=simplex_same,
            )
            spacing_khz = chosen.spacing_khz
            base_freq_hz = chosen.base_freq_hz
            iden_offset_mhz = chosen.offset_mhz
            bandwidth_khz = chosen.bandwidth_khz
        else:
            tx_hz = 462_618_750
            rx_hz = 467_618_750 if is_duplex else 462_618_750
            spacing_khz = 12.5
            base_freq_hz = tx_hz - 12_500
            iden_offset_mhz = 5.0
            bandwidth_khz = 12.5

        if not is_duplex:
            rx_hz = tx_hz

        eff_rx_hz = rx_hz + rx_tuning_hz
        eff_tx_hz = tx_hz + tx_tuning_hz

        return {
            "duplex": is_duplex,
            "simplex_same_freq": simplex_same,
            "channel_id": channel_id,
            "channel_no": channel_no,
            "channel_no_hex": format_channel_no(channel_no),
            "base_freq_hz": base_freq_hz,
            "spacing_khz": spacing_khz,
            "spacing_hz": int(round(spacing_khz / 0.125)) * 125,
            "bandwidth_khz": bandwidth_khz,
            "tx_freq_hz": tx_hz,
            "rx_freq_hz": rx_hz,
            "tx_freq_mhz": round(tx_hz / 1_000_000.0, 6),
            "rx_freq_mhz": round(rx_hz / 1_000_000.0, 6),
            "tx_offset_hz": rx_hz - tx_hz,
            "tx_offset_mhz": round((rx_hz - tx_hz) / 1_000_000.0, 6),
            "iden_offset_mhz": iden_offset_mhz,
            "rx_tuning_hz": rx_tuning_hz,
            "tx_tuning_hz": tx_tuning_hz,
            "effective_rx_hz": eff_rx_hz,
            "effective_tx_hz": eff_tx_hz,
            "effective_rx_mhz": round(eff_rx_hz / 1_000_000.0, 6),
            "effective_tx_mhz": round(eff_tx_hz / 1_000_000.0, 6),
        }

    def set_tuned_frequencies(
        self,
        tx_mhz: float,
        rx_mhz: float,
        conf_dict: Optional[Dict[str, Any]] = None,
        spacing_khz: float = 12.5,
    ) -> Tuple[int, int]:
        """
        Automatically calculate iden_table.dat using channelId=1 and channelNo=1 so
        BaseFrequency + (1 * ChannelSpacing) lands on the exact requested RX/TX frequencies in Hz,
        write iden_table.dat, and sync channelId: 1 / channelNo: '1' into config.yml.
        Returns: (channel_id=1, channel_no=1)
        """
        from dvm_nexus.config_engine.iden_calculator import (
            read_iden_table_file, format_iden_table, auto_calculate_single_iden_entry
        )
        is_duplex = True
        if conf_dict is not None:
            is_duplex = bool(conf_dict.get("system", {}).get("duplex", True))
        else:
            is_duplex = abs(rx_mhz - tx_mhz) >= 0.000001

        entry, _ = auto_calculate_single_iden_entry(
            rx_freq_mhz=rx_mhz,
            tx_freq_mhz=tx_mhz,
            spacing_khz=spacing_khz,
            bandwidth_khz=12.5,
            duplex=is_duplex,
            channel_id=1,
        )
        iden_entries = read_iden_table_file(self.iden_path)
        replaced = False
        for idx, existing in enumerate(iden_entries):
            if existing.channel_id == 1:
                iden_entries[idx] = entry
                replaced = True
                break
        if not replaced:
            iden_entries.append(entry)
        backup_manager.atomic_write(self.iden_path, format_iden_table(iden_entries))

        if conf_dict is not None:
            sys_conf = conf_dict.setdefault("system", {}).setdefault("config", {})
            sys_conf["channelId"] = 1
            sys_conf["channelNo"] = "1"
            if "voiceChNo" in sys_conf and isinstance(sys_conf["voiceChNo"], list) and len(sys_conf["voiceChNo"]) > 0:
                sys_conf["voiceChNo"][0]["channelId"] = 1
                sys_conf["voiceChNo"][0]["channelNo"] = "1"
                if not is_duplex:
                    sys_conf["voiceChNo"][0].pop("rxChannelId", None)
                    sys_conf["voiceChNo"][0].pop("rxChannelNo", None)
        return 1, 1

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
        channel_no = parse_channel_no(sys_conf.get("channelNo", "1"))

        # DMR and P25 specifics
        color_code = sys_conf.get("colorCode", 1)
        nac = sys_conf.get("nac", "293")
        net_id = sanitize_p25_wacn(sys_conf.get("netId", "BB800"))
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
