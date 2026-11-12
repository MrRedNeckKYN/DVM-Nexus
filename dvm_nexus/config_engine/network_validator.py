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


def validate_protocol_concurrency(config_data: Dict[str, Any]) -> List[str]:
    """
    Safety Block: Validates protocol mutual exclusion for ADF7021 / MMDVM hotspot modems.
    Hotspot firmwares reject multi-protocol concurrent operation with NAK 32 (HS_NO_DUAL_MODE).
    """
    errors: List[str] = []
    protocols = config_data.get("protocols", {})
    if not isinstance(protocols, dict):
        return errors

    active_protocols = []
    for proto in ("dmr", "p25", "nxdn"):
        p_dict = protocols.get(proto)
        if isinstance(p_dict, dict) and p_dict.get("enable") is True:
            active_protocols.append(proto.upper())

    if len(active_protocols) > 1:
        errors.append(
            f"Hotspot modem conflict: Multiple protocols enabled ({', '.join(active_protocols)}). "
            f"ADF7021 MMDVM hotspot hardware requires single-protocol mode; enabling more than one "
            f"triggers modem failure with NAK reason 32 (HS_NO_DUAL_MODE). Please enable only one protocol."
        )

    return errors


# ------------------------------------------------------------------------------
# Generic radio / hardware sanity validators
# ------------------------------------------------------------------------------
MODEM_MIN_MHZ = 70.0
MODEM_MAX_MHZ = 960.0
ALLOWED_UART_SPEEDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
_SERIAL_PORT_RE = re.compile(r"^(/dev/[A-Za-z0-9_./-]+|COM\d+)$")


def validate_frequencies(tx_mhz: float, rx_mhz: float) -> List[str]:
    """Sanity-check TX/RX frequencies against the ADF7021 modem tuning range."""
    errors: List[str] = []
    for label, value in (("TX", tx_mhz), ("RX", rx_mhz)):
        try:
            v = float(value)
        except (TypeError, ValueError):
            errors.append(f"{label} frequency '{value}' is not a number")
            continue
        if not (MODEM_MIN_MHZ <= v <= MODEM_MAX_MHZ):
            errors.append(
                f"{label} frequency {v} MHz is outside the supported modem range "
                f"({MODEM_MIN_MHZ:g}-{MODEM_MAX_MHZ:g} MHz)"
            )
    return errors


def validate_int_range(label: str, value: Any, low: int, high: int) -> List[str]:
    """Return an error list if value is not an integer within [low, high]."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return [f"{label} must be a whole number (got '{value}')"]
    if not (low <= v <= high):
        return [f"{label} must be between {low} and {high} (got {v})"]
    return []


def validate_port(label: str, value: Any) -> List[str]:
    """Validate a TCP/UDP port number."""
    return validate_int_range(label, value, 1, 65535)


def validate_serial_port(path: str, speed: Any = None) -> List[str]:
    """Validate a modem serial device path and optional baud rate."""
    errors: List[str] = []
    p = (path or "").strip()
    if not _SERIAL_PORT_RE.match(p):
        errors.append(f"Serial port '{p}' is not a valid device path (expected e.g. /dev/ttyAMA0 or /dev/ttyUSB0)")
    if speed is not None:
        try:
            if int(speed) not in ALLOWED_UART_SPEEDS:
                errors.append(f"UART speed {speed} is not supported (use one of {', '.join(map(str, ALLOWED_UART_SPEEDS))})")
        except (TypeError, ValueError):
            errors.append(f"UART speed '{speed}' must be a number")
    return errors


def validate_raw_config(data: Any) -> List[str]:
    """
    Structural validation for hand-edited config.yml content. Syntax-valid YAML can still stop
    DVMHost from starting, so refuse the handful of settings the Nexus safety net always enforces.
    """
    if not isinstance(data, dict):
        return ["config.yml must be a YAML mapping (key: value pairs) at the top level"]
    errors: List[str] = []
    if data.get("iAgreeNotToBeStupid") is not True:
        errors.append("'iAgreeNotToBeStupid' must be set to true or DVMHost refuses to start")
    if data.get("daemon") is True:
        errors.append("'daemon' must be false - DVMHost runs under systemd and must stay in the foreground")
    if not isinstance(data.get("system"), dict):
        errors.append("Missing required 'system:' section")
    if not isinstance(data.get("protocols"), dict):
        errors.append("Missing required 'protocols:' section")
    errors.extend(validate_protocol_concurrency(data))
    return errors
