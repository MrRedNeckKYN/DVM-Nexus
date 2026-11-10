"""
DVM Nexus - P25 Phase 1 Management Router
Handles P25 network settings, NAC, WACN, System ID, IDEN bandplans, and live over-the-air commands.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path
from typing import List, Dict, Any, Optional

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.network_validator import validate_p25_settings
from dvm_nexus.config_engine.iden_calculator import (
    read_iden_table_file,
    format_iden_table,
    IdenEntry,
    calculate_frequency_from_channel,
)
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.database import log_audit

router = APIRouter()


@router.get("/p25", response_class=HTMLResponse)
async def p25_page(request: Request):
    """Render the P25 management interface with protocol standby awareness."""
    conf = dvm_config_manager.load_config_dict()
    p25_conf = conf.get("protocols", {}).get("p25", {})
    sys_conf = conf.get("system", {}).get("config", {})
    modem_conf = conf.get("system", {}).get("modem", {})
    hotspot_conf = modem_conf.get("hotspot", {})

    active_proto = dvm_config_manager.get_active_protocol()
    is_standby = (active_proto == "dmr")

    iden_entries = read_iden_table_file(dvm_config_manager.iden_path)

    # Calculate active tuned frequency
    channel_id = sys_conf.get("channelId", 0)
    channel_no = sys_conf.get("channelNo", 1)
    active_tx_mhz = None
    active_rx_mhz = None
    for entry in iden_entries:
        if entry.channel_id == channel_id:
            tx_hz, rx_hz = calculate_frequency_from_channel(
                channel_no=channel_no,
                base_freq_hz=entry.base_freq_hz,
                spacing_khz=entry.spacing_khz,
                offset_mhz=entry.offset_mhz
            )
            active_tx_mhz = round(tx_hz / 1000000.0, 5)
            active_rx_mhz = round(rx_hz / 1000000.0, 5)
            break

    rids = dvm_config_manager.load_rid_acl()

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
            "iden_entries": iden_entries,
            "active_tx_mhz": active_tx_mhz,
            "active_rx_mhz": active_rx_mhz,
            "rids": rids,
        }
    )


@router.post("/p25/save", response_class=HTMLResponse)
async def p25_save(
    request: Request,
    enable: bool = Form(True),
    nac: str = Form("293"),
    tx_nac: str = Form(""),
    wacn: str = Form("BB800"),
    sys_id: str = Form("001"),
    rfss_id: int = Form(1),
    site_id: int = Form(1),
    channel_id: int = Form(0),
    channel_no: int = Form(1),
    call_hang: int = Form(5),
    tdu_preambles: int = Form(6),
    p25_corr_count: int = Form(2),
    p25_disc_bw: int = Form(0),
    p25_post_bw: int = Form(0),
):
    """Save P25 parameters safely, preserving profile settings without breaking standby mode."""
    errors = validate_p25_settings({
        "nac": nac,
        "netId": wacn,
        "sysId": sys_id,
        "rfssId": rfss_id,
        "siteId": site_id,
        "channelId": channel_id,
        "channelNo": channel_no,
    })
    if errors:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "; ".join(errors)}
        )

    active_proto = dvm_config_manager.get_active_protocol()
    is_standby = (active_proto == "dmr")

    def apply_p25_fields(c: Dict[str, Any]):
        c.setdefault("protocols", {}).setdefault("p25", {})
        c["protocols"]["p25"]["callHang"] = call_hang
        c["protocols"]["p25"]["tduPreambleCount"] = tdu_preambles

        c.setdefault("system", {}).setdefault("config", {})
        c["system"]["config"]["nac"] = nac
        if tx_nac.strip():
            c["system"]["config"]["txNAC"] = tx_nac.strip()
        elif "txNAC" in c["system"]["config"]:
            del c["system"]["config"]["txNAC"]

        c["system"]["config"]["netId"] = wacn.strip().upper()
        c["system"]["config"]["sysId"] = sys_id.strip().upper()
        c["system"]["config"]["rfssId"] = rfss_id
        c["system"]["config"]["siteId"] = site_id
        c["system"]["config"]["channelId"] = channel_id
        c["system"]["config"]["channelNo"] = channel_no

        c.setdefault("system", {}).setdefault("modem", {})
        c["system"]["modem"]["p25CorrCount"] = p25_corr_count
        c["system"]["modem"].setdefault("hotspot", {})
        c["system"]["modem"]["hotspot"]["p25DiscBWAdj"] = p25_disc_bw
        c["system"]["modem"]["hotspot"]["p25PostBWAdj"] = p25_post_bw

    conf = dvm_config_manager.load_config_dict()
    apply_p25_fields(conf)
    if is_standby:
        # Keep P25 disabled in active configuration so live DMR is undisturbed
        conf["protocols"]["p25"]["enable"] = False
        conf["protocols"].setdefault("dmr", {})["enable"] = True
    else:
        conf["protocols"]["p25"]["enable"] = enable
        if enable:
            conf["protocols"].setdefault("dmr", {})["enable"] = False

    dvm_config_manager.save_config_dict(conf, summary=f"Update P25 settings (NAC {nac}, WACN {wacn})")

    # Persist directly into P25 profile so settings are preserved when switching
    from dvm_nexus.config_engine.profile_manager import profile_manager
    profile_manager.sync_protocol_settings("p25", apply_p25_fields)

    log_audit("P25", "SAVE_CONFIG", f"Updated P25 parameters: NAC={nac}, WACN={wacn}, SysID={sys_id} (Standby: {is_standby})")

    if is_standby:
        msg = "P25 standby settings saved to profile! Radio remains on DMR. Settings will apply when you switch to P25."
    else:
        msg = "P25 settings saved successfully! Restart radio service to apply."

    return render_template(
        request,
        "components/toast.html",
        {"type": "success", "message": msg}
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
    rid: int = Form(...),
):
    """Dispatch over-the-air P25 RID command."""
    res = dvm_client.p25_rid_action(action=action, rid=rid)
    success = res.get("success", False)
    msg = f"P25 {action.upper()} to RID {rid}: {'Success' if success else res.get('error', 'Failed')}"
    log_audit("P25_OTA", action.upper(), f"Target RID: {rid}, Result: {msg}")

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
