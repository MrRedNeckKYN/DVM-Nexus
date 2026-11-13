"""
DVM Nexus - P25 Phase 1 Management Router
Handles P25 network settings, NAC, WACN, System ID, IDEN bandplans, and live over-the-air commands.
"""

import asyncio
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path
from typing import List, Dict, Any, Optional

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.dvm_yaml import (
    dvm_config_manager, is_banned_p25_wacn, sanitize_p25_wacn, normalize_p25_nac, normalize_p25_sys_id,
    parse_channel_no, format_channel_no,
)
from dvm_nexus.config_engine.network_validator import (
    validate_p25_settings, validate_frequencies, validate_int_range, validate_port,
)
from dvm_nexus.config_engine.iden_calculator import (
    read_iden_table_file,
    format_iden_table,
    IdenEntry,
    calculate_frequency_from_channel,
)
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.diagnostics import diagnostics_engine
from dvm_nexus.core.database import log_audit

router = APIRouter()


def build_p25_programming_summary(
    conf: Dict[str, Any],
    rf_verify: Dict[str, Any],
    iden_entries: List[IdenEntry],
) -> Dict[str, Any]:
    """
    Construct a structured summary of P25 Sites, Control Channels, Channel IDs,
    and Radio CPS codeplug values so users can easily program handheld/mobile radios.
    """
    sys_conf = conf.get("system", {}).get("config", {}) if isinstance(conf.get("system"), dict) else {}
    p25_conf = conf.get("protocols", {}).get("p25", {}) if isinstance(conf.get("protocols"), dict) else {}
    identity = conf.get("system", {}).get("identity", "NEXUS") if isinstance(conf.get("system"), dict) else "NEXUS"
    duplex = bool(conf.get("system", {}).get("duplex", False)) if isinstance(conf.get("system"), dict) else False

    cc_conf = p25_conf.get("control", {}) if isinstance(p25_conf, dict) else {}
    cc_enabled = bool(cc_conf.get("enable", False))
    cc_dedicated = bool(cc_conf.get("dedicated", False))

    if not duplex:
        cc_mode = "simplex"
        cc_mode_label = "1-Antenna Simplex (Conventional Direct Voice)"
    elif cc_dedicated:
        cc_mode = "dedicated"
        cc_mode_label = "Dedicated 24/7 Trunking Control Channel"
    elif cc_enabled:
        cc_mode = "non_dedicated"
        cc_mode_label = "Non-Dedicated CC + Voice (Combined TSBK)"
    else:
        cc_mode = "duplex_conv"
        cc_mode_label = "Full Duplex Conventional Repeater"

    raw_wacn = sanitize_p25_wacn(sys_conf.get("netId", "BB800"))
    try:
        wacn_dec = int(raw_wacn, 16)
    except ValueError:
        wacn_dec = int("BB800", 16)

    raw_sys = normalize_p25_sys_id(sys_conf.get("sysId", "001"))
    try:
        sys_dec = int(raw_sys, 16)
    except ValueError:
        sys_dec = 1

    raw_nac = normalize_p25_nac(sys_conf.get("nac", "293"))
    try:
        nac_dec = int(raw_nac, 16)
    except ValueError:
        nac_dec = int("293", 16)

    raw_tx_nac = normalize_p25_nac(sys_conf.get("txNAC")) if sys_conf.get("txNAC") else raw_nac
    try:
        tx_nac_dec = int(raw_tx_nac, 16)
    except ValueError:
        tx_nac_dec = nac_dec

    rfss_id = int(sys_conf.get("rfssId", 1))
    site_id = int(sys_conf.get("siteId", 1))
    authoritative = bool(sys_conf.get("authoritative", True))

    tx_freq_mhz = rf_verify.get("tx_freq_mhz", 462.618750)
    rx_freq_mhz = rf_verify.get("rx_freq_mhz", 462.618750 if not duplex else 467.618750)
    tx_freq_hz = rf_verify.get("tx_freq_hz", int(round(tx_freq_mhz * 1_000_000)))
    rx_freq_hz = rf_verify.get("rx_freq_hz", int(round(rx_freq_mhz * 1_000_000)))

    ch_id = rf_verify.get("channel_id", 1)
    ch_no = rf_verify.get("channel_no", 1)
    ch_no_hex = rf_verify.get("channel_no_hex", "$0001")
    base_freq_hz = rf_verify.get("base_freq_hz", tx_freq_hz - 12500)
    spacing_khz = rf_verify.get("spacing_khz", 12.5)
    spacing_hz = rf_verify.get("spacing_hz", 12500)
    tx_offset_mhz = rf_verify.get("tx_offset_mhz", 0.0 if not duplex else 5.0)
    bandwidth_khz = rf_verify.get("bandwidth_khz", 12.5)

    # Primary RF channel (MMDVM Hat TX -> Radio RX; MMDVM Hat RX <- Radio TX)
    primary_role = "Primary Control Channel (TSBK)" if (duplex and cc_enabled) else ("Simplex Voice" if not duplex else "Repeater Voice")
    channels_list = [{
        "name": "Ch 1 (Primary CC / Traffic)",
        "role": primary_role,
        "is_control": bool(duplex and cc_enabled),
        "channel_id": ch_id,
        "channel_no": ch_no,
        "channel_no_hex": ch_no_hex,
        "radio_rx_mhz": tx_freq_mhz,
        "radio_rx_hz": tx_freq_hz,
        "radio_tx_mhz": rx_freq_mhz,
        "radio_tx_hz": rx_freq_hz,
        "offset_mhz": tx_offset_mhz,
        "note": "MMDVM Hat TX -> Radio RX | MMDVM Hat RX <- Radio TX",
    }]

    # Additional Voice Channels if defined in voiceChNo
    iden_map = {e.channel_id: e for e in iden_entries}
    raw_vcs = sys_conf.get("voiceChNo", [])
    if isinstance(raw_vcs, list):
        for idx, vc in enumerate(raw_vcs, start=1):
            if not isinstance(vc, dict):
                continue
            vc_ch_id = int(vc.get("channelId", ch_id))
            vc_ch_no = parse_channel_no(vc.get("channelNo", 2))
            entry = iden_map.get(vc_ch_id)
            if entry:
                vc_tx_hz, vc_rx_hz = calculate_frequency_from_channel(
                    channel_no=vc_ch_no,
                    base_freq_hz=entry.base_freq_hz,
                    spacing_khz=entry.spacing_khz,
                    offset_mhz=entry.offset_mhz,
                    duplex=duplex,
                    simplex_same_freq=not duplex,
                )
                vc_radio_rx_mhz = round(vc_tx_hz / 1_000_000.0, 6)
                vc_radio_tx_mhz = round(vc_rx_hz / 1_000_000.0, 6)
            else:
                vc_tx_hz = tx_freq_hz
                vc_rx_hz = rx_freq_hz
                vc_radio_rx_mhz = tx_freq_mhz
                vc_radio_tx_mhz = rx_freq_mhz
            channels_list.append({
                "name": f"Voice Channel {idx}",
                "role": "Voice Channel (Payload)",
                "is_control": False,
                "channel_id": vc_ch_id,
                "channel_no": vc_ch_no,
                "channel_no_hex": format_channel_no(vc_ch_no),
                "radio_rx_mhz": vc_radio_rx_mhz,
                "radio_rx_hz": vc_tx_hz,
                "radio_tx_mhz": vc_radio_tx_mhz,
                "radio_tx_hz": vc_rx_hz,
                "offset_mhz": round((vc_rx_hz - vc_tx_hz) / 1_000_000.0, 6),
                "note": "Secondary voice channel grant target",
            })

    # IDEN Frequency Bandplan entries
    iden_list = []
    for e in iden_entries:
        iden_list.append({
            "channel_id": e.channel_id,
            "base_freq_hz": e.base_freq_hz,
            "base_freq_mhz": round(e.base_freq_hz / 1_000_000.0, 6),
            "spacing_khz": e.spacing_khz,
            "spacing_hz": int(round(e.spacing_khz * 1000.0)),
            "offset_mhz": e.offset_mhz,
            "offset_hz": int(round(e.offset_mhz * 1_000_000.0)),
            "bandwidth_khz": e.bandwidth_khz,
        })
    if not iden_list:
        iden_list.append({
            "channel_id": ch_id,
            "base_freq_hz": base_freq_hz,
            "base_freq_mhz": round(base_freq_hz / 1_000_000.0, 6),
            "spacing_khz": spacing_khz,
            "spacing_hz": spacing_hz,
            "offset_mhz": tx_offset_mhz,
            "offset_hz": int(round(tx_offset_mhz * 1_000_000.0)),
            "bandwidth_khz": bandwidth_khz,
        })

    return {
        "callsign": identity,
        "site_name": f"Site {site_id} (RFSS {rfss_id})",
        "wacn": raw_wacn,
        "wacn_hex": f"${raw_wacn}",
        "wacn_dec": wacn_dec,
        "sys_id": raw_sys,
        "sys_id_hex": f"${raw_sys}",
        "sys_id_dec": sys_dec,
        "rfss_id": rfss_id,
        "site_id": site_id,
        "nac": raw_nac,
        "nac_hex": f"${raw_nac}",
        "nac_dec": nac_dec,
        "tx_nac": raw_tx_nac,
        "tx_nac_hex": f"${raw_tx_nac}",
        "tx_nac_dec": tx_nac_dec,
        "authoritative": authoritative,
        "duplex": duplex,
        "cc_mode": cc_mode,
        "cc_mode_label": cc_mode_label,
        "cc_enabled": cc_enabled,
        "cc_dedicated": cc_dedicated,
        "cc_interval": int(cc_conf.get("interval", 300)),
        "cc_duration": int(cc_conf.get("duration", 1)),
        "radio_rx_mhz": tx_freq_mhz,
        "radio_rx_hz": tx_freq_hz,
        "radio_tx_mhz": rx_freq_mhz,
        "radio_tx_hz": rx_freq_hz,
        "channel_id": ch_id,
        "channel_no": ch_no,
        "channel_no_hex": ch_no_hex,
        "base_freq_hz": base_freq_hz,
        "base_freq_mhz": round(base_freq_hz / 1_000_000.0, 6),
        "spacing_khz": spacing_khz,
        "offset_mhz": tx_offset_mhz,
        "bandwidth_khz": bandwidth_khz,
        "channels": channels_list,
        "iden_tables": iden_list,
    }


@router.get("/p25", response_class=HTMLResponse)
async def p25_page(request: Request):
    """Render the comprehensive P25 management interface with protocol standby awareness."""
    conf = dvm_config_manager.load_config_dict()
    p25_conf = conf.get("protocols", {}).get("p25", {})
    sys_conf = conf.get("system", {}).get("config", {})
    modem_conf = conf.get("system", {}).get("modem", {})
    hotspot_conf = modem_conf.get("hotspot", {})
    net_conf = conf.get("network", {})
    cw_conf = conf.get("system", {}).get("cwId") or conf.get("cwId", {})
    identity = conf.get("system", {}).get("identity", "NEXUS")
    duplex = bool(conf.get("system", {}).get("duplex", False))

    radio_id_conf = conf.get("system", {}).get("radio_id", {})
    allow_unknown_rids = not bool(radio_id_conf.get("acl", False))
    tg_id_conf = conf.get("system", {}).get("talkgroup_id", {})
    allow_all_tgs = not bool(tg_id_conf.get("acl", False))

    active_proto = dvm_config_manager.get_active_protocol()
    is_standby = (active_proto == "dmr")

    iden_entries = read_iden_table_file(dvm_config_manager.iden_path)
    tx_freq_mhz, iden_rx_freq_mhz = dvm_config_manager.get_tuned_frequencies()
    rx_freq_mhz = iden_rx_freq_mhz if duplex else tx_freq_mhz
    rf_verify = dvm_config_manager.get_rf_verification(conf)
    modem_diag = diagnostics_engine.get_modem_firmware_summary(conf)
    talkgroups = dvm_config_manager.load_talkgroup_rules()
    rids = dvm_config_manager.load_rid_acl()
    p25_prog = build_p25_programming_summary(conf, rf_verify, iden_entries)

    return render_template(
        request,
        "p25.html",
        {
            "title": "P25 Management",
            "active_tab": "p25",
            "active_protocol": active_proto,
            "is_standby": is_standby,
            "p25": p25_conf,
            "sys": sys_conf,
            "modem": modem_conf,
            "hotspot": hotspot_conf,
            "net": net_conf,
            "cw": cw_conf,
            "identity": identity,
            "duplex": duplex,
            "allow_unknown_rids": allow_unknown_rids,
            "allow_all_tgs": allow_all_tgs,
            "iden_entries": iden_entries,
            "active_tx_mhz": tx_freq_mhz,
            "active_rx_mhz": rx_freq_mhz,
            "duplex_rx_freq_mhz": iden_rx_freq_mhz,
            "spacing_khz": rf_verify.get("spacing_khz", 12.5),
            "rf_verify": rf_verify,
            "p25_prog": p25_prog,
            "modem_diag": modem_diag,
            "talkgroups": talkgroups,
            "rids": rids,
            "config_rev": dvm_config_manager.get_config_revision(),
        }
    )


@router.post("/p25/save", response_class=HTMLResponse)
async def p25_save(
    request: Request,
    enable: bool = Form(True),
    # Core Station & Frequency
    callsign: str = Form("NEXUS"),
    duplex: bool = Form(False),
    tx_freq_mhz: Optional[float] = Form(None),
    rx_freq_mhz: Optional[float] = Form(None),
    tx_freq: Optional[float] = Form(None),
    rx_freq: Optional[float] = Form(None),
    txFrequencyMHz: Optional[float] = Form(None),
    rxFrequencyMHz: Optional[float] = Form(None),
    spacing_khz: float = Form(12.5),
    channelSpacingKHz: Optional[float] = Form(None),
    cw_id_enable: bool = Form(True),
    cw_id_time: int = Form(15),
    # P25 Protocol & ACLs
    nac: str = Form("293"),
    tx_nac: str = Form(""),
    wacn: str = Form("BB800"),
    sys_id: str = Form("001"),
    rfss_id: int = Form(1),
    site_id: int = Form(1),
    call_hang: int = Form(5),
    tdu_preambles: int = Form(6),
    immediate_call_term: bool = Form(True),
    authoritative: bool = Form(True),
    default_net_idle_tg: int = Form(0),
    allow_unknown_rids: bool = Form(True),
    allow_all_tgs: bool = Form(True),
    # Master Network Linking
    net_enable: bool = Form(False),
    net_id: int = Form(100000),
    net_address: str = Form("127.0.0.1"),
    net_port: int = Form(62031),
    net_password: str = Form("PASSWORD"),
    # Advanced P25 Parameters & Control Channel
    silence_threshold: int = Form(124),
    frame_loss_threshold: int = Form(6),
    queue_size: int = Form(12),
    cc_mode: Optional[str] = Form(None),
    cc_enable: bool = Form(False),
    cc_broadcast: bool = Form(True),
    cc_interval: int = Form(300),
    cc_duration: int = Form(1),
    cc_ack_requests: bool = Form(True),
    cc_notify_active_tg: bool = Form(False),
    cc_dedicated: bool = Form(False),
    control_only: bool = Form(False),
    disable_grant_src_check: bool = Form(False),
    inhibit_unauthorized: bool = Form(False),
    disable_deny_response: bool = Form(False),
    repeat_data_packet: bool = Form(True),
    unit_to_unit_avail_check: bool = Form(False),
    ignore_affiliation_check: bool = Form(False),
    verify_aff: bool = Form(False),
    verify_reg: bool = Form(False),
    legacy_group_grnt: bool = Form(True),
    legacy_group_reg: bool = Form(False),
    conv_net_grant_demand: bool = Form(False),
    demand_unit_reg_for_refused_aff: bool = Form(True),
    # Modem & ADF7021 Tuning
    tx_tuning: int = Form(0),
    rx_tuning: int = Form(0),
    p25_corr_count: int = Form(2),
    p25_disc_bw: int = Form(0),
    p25_post_bw: int = Form(0),
    adf_gain_mode: int = Form(2),
    afc_enable: bool = Form(True),
    tx_level: int = Form(50),
    config_rev: str = Form(""),
):
    """Save comprehensive P25 parameters safely, auto-calculating iden_table.dat and restarting dvmhost."""
    if config_rev and config_rev != dvm_config_manager.get_config_revision():
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Not saved: config.yml changed since this page was loaded (another tab/user, auto-repair or a restore). Reload the page to see the latest values, then re-apply your edit."}
        )

    eff_spacing_khz = float(channelSpacingKHz if channelSpacingKHz is not None else spacing_khz)
    resolved_rx = rxFrequencyMHz if rxFrequencyMHz is not None else (rx_freq_mhz if rx_freq_mhz is not None else rx_freq)
    resolved_tx = txFrequencyMHz if txFrequencyMHz is not None else (tx_freq_mhz if tx_freq_mhz is not None else tx_freq)

    if resolved_rx is None and resolved_tx is None:
        resolved_tx = 462.61875
        resolved_rx = 462.61875 if not duplex else 467.61875
    elif resolved_rx is None:
        resolved_rx = resolved_tx
    elif resolved_tx is None:
        resolved_tx = resolved_rx

    tx_freq_mhz = float(resolved_tx)
    rx_freq_mhz = float(resolved_rx)

    # Resolve unified cc_mode selector if provided
    if cc_mode is not None:
        mode_val = cc_mode.strip().lower()
        if mode_val == "dedicated":
            cc_enable = True
            cc_dedicated = True
            cc_broadcast = True
            control_only = False
        elif mode_val == "non_dedicated":
            cc_enable = True
            cc_dedicated = False
        else:
            cc_enable = False
            cc_dedicated = False
            control_only = False

    split_notice = ""
    # DVMHost requirement: P25 Control Channel requires Full Duplex
    if not duplex:
        if rxFrequencyMHz is not None or rx_freq_mhz is not None:
            tx_freq_mhz = rx_freq_mhz
        else:
            rx_freq_mhz = tx_freq_mhz
        cc_enable = False
        cc_dedicated = False
        control_only = False
        verify_aff = False
        verify_reg = False
    else:
        if not cc_enable:
            verify_aff = False
            verify_reg = False
        if abs(tx_freq_mhz - rx_freq_mhz) < 0.0001:
            if 400.0 <= tx_freq_mhz <= 480.0:
                rx_freq_mhz = round(tx_freq_mhz + 5.0, 6)
            elif 136.0 <= tx_freq_mhz <= 174.0:
                rx_freq_mhz = round(tx_freq_mhz + 0.6, 6)
            elif 800.0 <= tx_freq_mhz <= 870.0:
                rx_freq_mhz = round(tx_freq_mhz - 45.0, 6)
            elif 760.0 <= tx_freq_mhz <= 780.0:
                rx_freq_mhz = round(tx_freq_mhz + 30.0, 6)
            else:
                rx_freq_mhz = round(tx_freq_mhz + 5.0, 6)
            split_notice = f" (Full Duplex requires split RX/TX; set RX to {rx_freq_mhz:.6f} MHz — select Simplex for same-frequency hats)"

    # Normalize NAC, SysId, and WACN (accepting 0x293, $293, 293, etc.)
    wacn = sanitize_p25_wacn(wacn)

    errors = validate_p25_settings({
        "nac": nac,
        "netId": wacn,
        "sysId": sys_id,
        "rfssId": rfss_id,
        "siteId": site_id,
    })
    errors += validate_frequencies(tx_freq_mhz, rx_freq_mhz)
    errors += validate_int_range("TX level", tx_level, 0, 100)
    errors += validate_int_range("CW ID interval", cw_id_time, 1, 1440)
    errors += validate_int_range("Call hang time", call_hang, 0, 600)
    if net_enable:
        errors += validate_port("Network port", net_port)
    if errors:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Not saved - " + "; ".join(errors)}
        )

    norm_nac = normalize_p25_nac(nac)
    norm_sys_id = normalize_p25_sys_id(sys_id)

    def apply_p25_fields(c: Dict[str, Any]):
        c.setdefault("system", {})
        c["system"]["identity"] = callsign.strip() or "NEXUS"
        c["system"]["duplex"] = duplex
        c["system"]["simplexSameFrequency"] = not duplex
        c["system"]["fixedMode"] = True

        # Subscriber ID & Talkgroup ACLs (allowUnknown: true prevents silent rejection of unlisted radios)
        c["system"].setdefault("radio_id", {})
        c["system"]["radio_id"]["acl"] = not allow_unknown_rids
        c["system"]["radio_id"]["allowUnknown"] = allow_unknown_rids
        c["system"].setdefault("talkgroup_id", {})
        c["system"]["talkgroup_id"]["acl"] = not allow_all_tgs

        for cw_target in (c.setdefault("cwId", {}), c["system"].setdefault("cwId", {})):
            cw_target["enable"] = cw_id_enable
            cw_target["time"] = cw_id_time
            cw_target["callsign"] = callsign.strip() or "NEXUS"

        c.setdefault("network", {})
        c["network"]["enable"] = net_enable
        c["network"]["id"] = net_id
        c["network"]["address"] = net_address.strip()
        c["network"]["port"] = net_port
        c["network"]["password"] = net_password.strip()

        c.setdefault("protocols", {}).setdefault("p25", {})
        c["protocols"]["p25"]["nac"] = norm_nac
        c["protocols"]["p25"]["sysId"] = norm_sys_id
        c["protocols"]["p25"]["wacn"] = wacn
        c["protocols"]["p25"]["netId"] = wacn
        c["protocols"]["p25"]["callHang"] = call_hang
        c["protocols"]["p25"]["tduPreambleCount"] = tdu_preambles
        c["protocols"]["p25"]["immediateCallTerm"] = immediate_call_term
        c["protocols"]["p25"]["silenceThreshold"] = silence_threshold
        c["protocols"]["p25"]["frameLossThreshold"] = frame_loss_threshold
        c["protocols"]["p25"]["queueSize"] = queue_size
        c["protocols"]["p25"]["inhibitUnauthorized"] = inhibit_unauthorized
        c["protocols"]["p25"]["disableDenyResponse"] = disable_deny_response
        c["protocols"]["p25"]["repeatDataPacket"] = repeat_data_packet
        c["protocols"]["p25"]["unitToUnitAvailCheck"] = unit_to_unit_avail_check
        c["protocols"]["p25"]["ignoreAffiliationCheck"] = ignore_affiliation_check
        c["protocols"]["p25"]["verifyAff"] = verify_aff
        c["protocols"]["p25"]["verifyReg"] = verify_reg
        c["protocols"]["p25"]["legacyGroupGrnt"] = legacy_group_grnt
        c["protocols"]["p25"]["legacyGroupReg"] = legacy_group_reg
        c["protocols"]["p25"]["convNetGrantDemand"] = conv_net_grant_demand
        c["protocols"]["p25"]["demandUnitRegForRefusedAff"] = demand_unit_reg_for_refused_aff
        c["protocols"]["p25"]["controlOnly"] = control_only

        c["protocols"]["p25"].setdefault("control", {})
        c["protocols"]["p25"]["control"]["enable"] = cc_enable
        c["protocols"]["p25"]["control"]["broadcast"] = cc_broadcast
        c["protocols"]["p25"]["control"]["interval"] = cc_interval
        c["protocols"]["p25"]["control"]["duration"] = cc_duration
        c["protocols"]["p25"]["control"]["ackRequests"] = cc_ack_requests
        c["protocols"]["p25"]["control"]["notifyActiveTG"] = cc_notify_active_tg
        c["protocols"]["p25"]["control"]["dedicated"] = cc_dedicated
        c["protocols"]["p25"]["control"]["disableGrantSourceIdCheck"] = disable_grant_src_check

        c.setdefault("system", {}).setdefault("config", {})
        c["system"]["config"]["nac"] = norm_nac
        if tx_nac.strip():
            c["system"]["config"]["txNAC"] = normalize_p25_nac(tx_nac.strip())
        elif "txNAC" in c["system"]["config"]:
            del c["system"]["config"]["txNAC"]

        c["system"]["config"]["netId"] = wacn
        c["system"]["config"]["sysId"] = norm_sys_id
        c["system"]["config"]["rfssId"] = rfss_id
        c["system"]["config"]["siteId"] = site_id
        c["system"]["config"]["authoritative"] = authoritative
        c["system"]["config"]["defaultNetIdleTalkgroup"] = default_net_idle_tg

        c.setdefault("system", {}).setdefault("modem", {})
        c["system"]["modem"]["p25CorrCount"] = p25_corr_count
        c["system"]["modem"]["txLevel"] = tx_level
        c["system"]["modem"].setdefault("hotspot", {})
        c["system"]["modem"]["hotspot"]["txTuning"] = tx_tuning
        c["system"]["modem"]["hotspot"]["rxTuning"] = rx_tuning
        c["system"]["modem"]["hotspot"]["p25DiscBWAdj"] = p25_disc_bw
        c["system"]["modem"]["hotspot"]["p25PostBWAdj"] = p25_post_bw
        c["system"]["modem"]["hotspot"]["adfGainMode"] = adf_gain_mode
        c["system"]["modem"]["hotspot"]["afcEnable"] = afc_enable

        # Auto-calculate iden_table.dat (Channel ID 1, Channel No 1)
        dvm_config_manager.set_tuned_frequencies(tx_freq_mhz, rx_freq_mhz, c, spacing_khz=eff_spacing_khz)

    conf = dvm_config_manager.load_config_dict()
    apply_p25_fields(conf)

    # Always enforce P25 as active protocol when saving P25
    conf.setdefault("protocols", {})
    conf["protocols"].setdefault("p25", {})["enable"] = True
    conf["protocols"].setdefault("dmr", {})["enable"] = False

    dvm_config_manager.save_config_dict(conf, summary=f"Update active P25 settings (NAC {norm_nac}, RX {rx_freq_mhz} / TX {tx_freq_mhz} MHz)")

    # Also persist directly into P25 profile backup so settings are preserved if switching
    from dvm_nexus.config_engine.profile_manager import profile_manager
    profile_manager.sync_protocol_settings("p25", apply_p25_fields)

    # Cleanly restart dvmhost (and dvmfne if active) so new YAML/DAT settings take effect immediately
    await asyncio.to_thread(service_controller.restart)
    diagnostics_engine.invalidate_cache()

    rf_v = dvm_config_manager.get_rf_verification(conf)
    log_audit(
        "P25",
        "SAVE_CONFIG",
        f"Updated P25: NAC={norm_nac}, WACN={wacn}, Duplex={duplex}, RX={rf_v['rx_freq_hz']} Hz, TX={rf_v['tx_freq_hz']} Hz, Base={rf_v['base_freq_hz']} Hz"
    )

    msg = (
        f"✓ P25 settings saved to active config & service reloaded — "
        f"RX: {rf_v['rx_freq_hz']:,} Hz | TX: {rf_v['tx_freq_hz']:,} Hz "
        f"(ChId 1 / ChNo 1, NAC ${norm_nac}){split_notice}"
    )

    return render_template(
        request,
        "components/toast_saved.html",
        {"type": "success", "message": msg, "config_rev": dvm_config_manager.get_config_revision()}
    )


# Talkgroup rules endpoints for P25
@router.post("/p25/talkgroups/add", response_class=HTMLResponse)
async def p25_tg_add(
    request: Request,
    tgid: int = Form(...),
    name: str = Form(...),
    parrot: bool = Form(False),
):
    """Add a talkgroup to talkgroup_rules.yml for P25."""
    rules = dvm_config_manager.load_talkgroup_rules()
    for r in rules:
        if r.get("id") == tgid or r.get("tgid") == tgid:
            return render_template(
                request,
                "components/toast.html",
                {"type": "error", "message": f"Talkgroup {tgid} already exists"}
            )

    rules.append({"id": tgid, "tgid": tgid, "name": name, "slot": 1, "parrot": parrot, "active": True})
    dvm_config_manager.save_talkgroup_rules(rules, summary=f"Add P25 talkgroup {tgid} ({name})")
    log_audit("P25", "ADD_TG", f"Added P25 TG {tgid} ({name}) Parrot={parrot}")

    return render_template(
        request,
        "components/p25_tg_table.html",
        {"talkgroups": rules, "toast": {"type": "success", "message": f"Added Talkgroup {tgid} ({name})"}}
    )


@router.post("/p25/talkgroups/delete/{tgid}", response_class=HTMLResponse)
async def p25_tg_delete(request: Request, tgid: int):
    """Delete a talkgroup from talkgroup_rules.yml."""
    rules = dvm_config_manager.load_talkgroup_rules()
    filtered = [r for r in rules if r.get("id") != tgid and r.get("tgid") != tgid]
    dvm_config_manager.save_talkgroup_rules(filtered, summary=f"Delete P25 talkgroup {tgid}")
    log_audit("P25", "DEL_TG", f"Deleted TG {tgid}")

    return render_template(
        request,
        "components/p25_tg_table.html",
        {"talkgroups": filtered, "toast": {"type": "success", "message": f"Deleted Talkgroup {tgid}"}}
    )


# IDEN Table management endpoints
@router.post("/p25/iden/save", response_class=HTMLResponse)
async def p25_iden_save(
    request: Request,
    channel_id: int = Form(...),
    base_freq_hz: int = Form(...),
    spacing_khz: float = Form(6.25),
    offset_mhz: float = Form(0.0),
    bandwidth_khz: float = Form(12.5),
):
    """Add or update an IDEN bandplan entry in iden_table.dat."""
    # DVMHost requires a non-zero TX offset in iden_table.dat. Replaces 0.0 with 5.0 MHz.
    if abs(offset_mhz) < 0.0001:
        offset_mhz = 5.000 if (400000000 <= base_freq_hz <= 480000000) else 5.000

    entries = read_iden_table_file(dvm_config_manager.iden_path)
    updated = False
    for i, e in enumerate(entries):
        if e.channel_id == channel_id:
            entries[i] = IdenEntry(
                channel_id=channel_id,
                base_freq_hz=base_freq_hz,
                spacing_khz=spacing_khz,
                offset_mhz=offset_mhz,
                bandwidth_khz=bandwidth_khz
            )
            updated = True
            break

    if not updated:
        entries.append(IdenEntry(
            channel_id=channel_id,
            base_freq_hz=base_freq_hz,
            spacing_khz=spacing_khz,
            offset_mhz=offset_mhz,
            bandwidth_khz=bandwidth_khz
        ))

    backup_manager.create_backup(summary=f"Update IDEN Bandplan Channel {channel_id}", changed_files=["iden_table.dat"])
    new_content = format_iden_table(entries)
    backup_manager.atomic_write(dvm_config_manager.iden_path, new_content)
    log_audit("P25", "SAVE_IDEN", f"Updated IDEN Channel {channel_id} (Base: {base_freq_hz} Hz, Offset: {offset_mhz} MHz)")

    return render_template(
        request,
        "components/p25_iden_table.html",
        {"iden_entries": entries, "toast": {"type": "success", "message": f"Saved IDEN Channel {channel_id}"}}
    )


@router.post("/p25/iden/delete/{chid}", response_class=HTMLResponse)
async def p25_iden_delete(request: Request, chid: int):
    """Delete an IDEN bandplan entry."""
    # DVMHost requires a non-zero TX offset in iden_table.dat. Replaces 0.0 with 5.0 MHz.
    if abs(offset_mhz) < 0.0001:
        offset_mhz = 5.000 if (400000000 <= base_freq_hz <= 480000000) else 5.000

    entries = read_iden_table_file(dvm_config_manager.iden_path)
    filtered = [e for e in entries if e.channel_id != chid]
    backup_manager.create_backup(summary=f"Delete IDEN Channel {chid}", changed_files=["iden_table.dat"])
    new_content = format_iden_table(filtered)
    backup_manager.atomic_write(dvm_config_manager.iden_path, new_content)
    log_audit("P25", "DEL_IDEN", f"Deleted IDEN Channel {chid}")

    return render_template(
        request,
        "components/p25_iden_table.html",
        {"iden_entries": filtered, "toast": {"type": "success", "message": f"Deleted IDEN Channel {chid}"}}
    )


# Live Over-The-Air P25 RID commands
@router.post("/p25/rids/action", response_class=HTMLResponse)
async def p25_rid_action(
    request: Request,
    action: str = Form(...),
    rid: int = Form(0),
    tg_id: int = Form(1),
):
    """Dispatch over-the-air P25 RID command."""
    cmd = action.lower().strip()
    conf = dvm_config_manager.load_config_dict()
    is_duplex = bool(conf.get("system", {}).get("duplex", True))
    cc_enabled = bool(conf.get("protocols", {}).get("p25", {}).get("control", {}).get("enable", False))

    if not is_duplex or not cc_enabled:
        msg = "DVMHost requires Full Duplex + P25 Control Channel (protocols.p25.control.enable: true) to transmit P25 TSBK Over-The-Air commands. Select Card 3 (Full Duplex + Non-Dedicated CC) below, Save, and Restart Radio."
        log_audit("P25_OTA", cmd.upper(), f"Blocked (duplex={is_duplex}, cc={cc_enabled}): {msg}")
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": msg},
        )

    if cmd not in ("cc", "cc-burst") and rid <= 0:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Enter a valid Target Radio ID (RID > 0)."},
        )

    res = dvm_client.p25_rid_action(action=cmd, rid=rid, tg_id=tg_id)
    success = res.get("success", False)
    if cmd in ("cc", "cc-burst"):
        msg = f"P25 Control Channel Burst: {'Transmitted over RF' if success else res.get('error', 'Failed')}"
    elif cmd == "dyn-regrp":
        msg = f"P25 Dynamic Regroup (RID {rid} -> TG {tg_id}): {'Transmitted over RF' if success else res.get('error', 'Failed')}"
    else:
        msg = f"P25 {cmd.upper()} to RID {rid}: {'Transmitted over RF' if success else res.get('error', 'Failed')}"
    log_audit("P25_OTA", cmd.upper(), f"Target RID: {rid}, TG: {tg_id}, Result: {msg}")

    return render_template(
        request,
        "components/toast.html",
        {"type": "success" if success else "error", "message": msg}
    )


@router.post("/p25/send-sms", response_class=HTMLResponse)
async def p25_send_sms(
    request: Request,
    target_id: int = Form(...),
    target_type: str = Form("group"),
    text: str = Form(...),
    forward_to_mesh: Optional[str] = Form("1"),
):
    """Send P25 Text Message (PDU/TMS) to Talkgroup or Unit ID, optionally forwarding to Meshtastic."""
    is_group = target_type == "group"
    from dvm_nexus.meshtastic.text_gateway import text_gateway
    res = text_gateway.send_text_from_web(
        protocol="p25",
        dest_id=target_id,
        is_group=is_group,
        text=text,
        forward_to_mesh=True if forward_to_mesh == "1" else False
    )
    success = res.get("success", False)
    msg = f"P25 TMS sent to {'TG' if is_group else 'Unit'} {target_id}: '{text}'" + (" (Mirrored to Mesh)" if forward_to_mesh == "1" else "")
    return render_template(
        request,
        "components/toast.html",
        {"type": "success" if success else "error", "message": msg}
    )
