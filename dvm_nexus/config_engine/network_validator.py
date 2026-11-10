"""
DVM Nexus - Network & Protocol Schema Validator
Enforces DVMHost and protocol standard limits for DMR and P25 networks.
"""

import re
from typing import Dict, Any, List, Tuple


def validate_dmr_settings(data: Dict[str, Any]) -> List[str]:
    """Validate DMR specific settings."""
    errors: List[str] = []

    # Color code (0 - 15)
    cc = data.get("colorCode")
    if cc is not None:
        try:
            val = int(cc)
            if not (0 <= val <= 15):
                errors.append(f"DMR Color Code must be between 0 and 15 (got {val})")
        except ValueError:
            errors.append(f"Invalid Color Code '{cc}': must be an integer")

    # Network ID
    net_id = data.get("dmrNetId")
    if net_id is not None:
        try:
            val = int(net_id)
            if not (1 <= val <= 16777215):
                errors.append(f"DMR Network ID must be between 1 and 16,777,215 (got {val})")
        except ValueError:
            errors.append(f"Invalid DMR Network ID '{net_id}'")

    # Site ID
    site_id = data.get("siteId")
    if site_id is not None:
        try:
            val = int(site_id)
            if not (1 <= val <= 1023):
                errors.append(f"Site ID must be between 1 and 1023 (got {val})")
        except ValueError:
            errors.append(f"Invalid Site ID '{site_id}'")

    return errors


def validate_p25_settings(data: Dict[str, Any]) -> List[str]:
    """Validate P25 Phase 1 specific settings."""
    errors: List[str] = []

    # NAC (12-bit hex/dec)
    nac = str(data.get("nac", "")).strip()
    if nac:
        # Check if hex string (e.g. "293" or "0x293")
        try:
            val = int(nac, 16) if not nac.isdigit() else int(nac)
            if not (1 <= val <= 0xFFF):
                errors.append(f"P25 NAC must be a 12-bit value between 0x001 and 0xFFF (got {nac})")
        except ValueError:
            errors.append(f"Invalid NAC '{nac}': must be valid hex or decimal")

    # WACN (20-bit hex string)
    wacn = str(data.get("netId", "")).strip().upper()
    if wacn:
        try:
            val = int(wacn, 16)
            if not (0 <= val <= 0xFFFFF):
                errors.append(f"P25 WACN must be a 20-bit hex value up to 5 characters (got {wacn})")
        except ValueError:
            errors.append(f"Invalid WACN '{wacn}': must be hexadecimal")

    # System ID (12-bit hex)
    sys_id = str(data.get("sysId", "")).strip().upper()
    if sys_id:
        try:
            val = int(sys_id, 16)
            if not (1 <= val <= 0xFFF):
                errors.append(f"P25 System ID must be a 12-bit hex value (got {sys_id})")
        except ValueError:
            errors.append(f"Invalid System ID '{sys_id}': must be hexadecimal")

    # RFSS ID (1 - 255)
    rfss = data.get("rfssId")
    if rfss is not None:
        try:
            val = int(rfss)
            if not (1 <= val <= 255):
                errors.append(f"P25 RFSS ID must be between 1 and 255 (got {val})")
        except ValueError:
            errors.append(f"Invalid RFSS ID '{rfss}'")

    # Channel ID (0 - 15)
    chid = data.get("channelId")
    if chid is not None:
        try:
            val = int(chid)
            if not (0 <= val <= 15):
                errors.append(f"Channel ID must be between 0 and 15 (got {val})")
        except ValueError:
            errors.append(f"Invalid Channel ID '{chid}'")

    # Channel No (1 - 4095)
    chno = data.get("channelNo")
    if chno is not None:
        try:
            val = int(chno)
            if not (1 <= val <= 4095):
                errors.append(f"Channel Number must be between 1 and 4095 (got {val})")
        except ValueError:
            errors.append(f"Invalid Channel Number '{chno}'")

    return errors


def validate_radio_id(rid: int) -> bool:
    """Validate Radio ID (1 to 16,777,215)."""
    return 1 <= int(rid) <= 16777215


def validate_talkgroup(tgid: int) -> bool:
    """Validate Talkgroup ID (1 to 16,777,215)."""
    return 1 <= int(tgid) <= 16777215
