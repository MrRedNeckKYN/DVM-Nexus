"""
DVM Nexus - DMR Management Router
Handles DMR parameters, talkgroup rules, Radio ID ACLs, and live over-the-air commands.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path
from typing import List, Dict, Any, Optional

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.network_validator import validate_dmr_settings
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.database import log_audit

router = APIRouter()


@router.get("/dmr", response_class=HTMLResponse)
async def dmr_page(request: Request):
    """Render the comprehensive DMR management interface with protocol standby awareness."""
    conf = dvm_config_manager.load_config_dict()
    dmr_conf = conf.get("protocols", {}).get("dmr", {})
    sys_conf = conf.get("system", {}).get("config", {})
    modem_conf = conf.get("system", {}).get("modem", {})
    hotspot_conf = modem_conf.get("hotspot", {})
    net_conf = conf.get("network", {})
    cw_conf = conf.get("cwId", {})
    identity = conf.get("system", {}).get("identity", "NEXUS")
    duplex = conf.get("system", {}).get("duplex", True)

    active_proto = dvm_config_manager.get_active_protocol()
    is_standby = (active_proto == "p25")

    tx_freq_mhz, rx_freq_mhz = dvm_config_manager.get_tuned_frequencies()
    if not duplex:
        rx_freq_mhz = tx_freq_mhz
    talkgroups = dvm_config_manager.load_talkgroup_rules()
    rids = dvm_config_manager.load_rid_acl()

    return render_template(
        request,
        "dmr.html",
        {
            "title": "DMR Management",
            "active_tab": "dmr",
            "active_protocol": active_proto,
            "is_standby": is_standby,
            "dmr": dmr_conf,
            "sys": sys_conf,
            "modem": modem_conf,
            "hotspot": hotspot_conf,
            "net": net_conf,
            "cw": cw_conf,
            "identity": identity,
            "duplex": duplex,
            "tx_freq_mhz": tx_freq_mhz,
            "rx_freq_mhz": rx_freq_mhz,
            "talkgroups": talkgroups,
            "rids": rids,
        }
    )


@router.post("/dmr/save", response_class=HTMLResponse)
async def dmr_save(
    request: Request,
    enable: bool = Form(True),
    # Core Station & Frequency
    callsign: str = Form("NEXUS"),
    duplex: bool = Form(True),
    tx_freq_mhz: float = Form(462.61875),
    rx_freq_mhz: float = Form(467.61875),
    cw_id_enable: bool = Form(True),
    cw_id_time: int = Form(15),
    # DMR Protocol
    color_code: int = Form(1),
    dmr_net_id: int = Form(1),
    site_id: int = Form(1),
    default_net_idle_tg: int = Form(0),
    call_hang: int = Form(5),
    tx_hang: int = Form(8),
    # Master Network Linking
    net_enable: bool = Form(False),
    net_id: int = Form(100000),
    net_address: str = Form("127.0.0.1"),
    net_port: int = Form(62031),
    net_password: str = Form("PASSWORD"),
    slot1: bool = Form(True),
    slot2: bool = Form(True),
    jitter: int = Form(360),
    # Advanced DMR Parameters
    silence_threshold: int = Form(21),
    frame_loss_threshold: int = Form(2),
    queue_size: int = Form(31),
    beacon_enable: bool = Form(False),
    beacon_interval: int = Form(60),
    beacon_duration: int = Form(3),
    tscc_enable: bool = Form(False),
    tscc_slot: int = Form(1),
    tscc_dedicated: bool = Form(False),
    repeat_data_packet: bool = Form(True),
    dump_ta_data: bool = Form(True),
    embedded_lc_only: bool = Form(False),
    verify_reg: bool = Form(False),
    ignore_affiliation_check: bool = Form(False),
    legacy_group_reg: bool = Form(False),
    conv_net_grant_demand: bool = Form(False),
    # Modem & ADF7021 Tuning
    tx_tuning: int = Form(0),
    rx_tuning: int = Form(0),
    dmr_disc_bw: int = Form(0),
    dmr_post_bw: int = Form(0),
    adf_gain_mode: int = Form(2),
    afc_enable: bool = Form(True),
    dmr_rx_delay: int = Form(7),
    tx_level: int = Form(50),
):
    """Save comprehensive DMR parameters safely, preserving profile settings without breaking standby mode."""
    errors = validate_dmr_settings({"colorCode": color_code, "dmrNetId": dmr_net_id, "siteId": site_id})
    if errors:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "; ".join(errors)}
        )

    if not duplex:
        rx_freq_mhz = tx_freq_mhz

    active_proto = dvm_config_manager.get_active_protocol()
    is_standby = (active_proto == "p25")

    def apply_dmr_fields(c: Dict[str, Any]):
        c.setdefault("system", {})
        c["system"]["identity"] = callsign.strip() or "NEXUS"
        c["system"]["duplex"] = duplex

        c.setdefault("cwId", {})
        c["cwId"]["enable"] = cw_id_enable
        c["cwId"]["time"] = cw_id_time
        c["cwId"]["callsign"] = callsign.strip() or "NEXUS"

        c.setdefault("network", {})
        c["network"]["enable"] = net_enable
        c["network"]["id"] = net_id
        c["network"]["address"] = net_address.strip()
        c["network"]["port"] = net_port
        c["network"]["password"] = net_password.strip()
        c["network"]["slot1"] = slot1
        c["network"]["slot2"] = slot2
        c["network"]["jitter"] = jitter

        c.setdefault("protocols", {}).setdefault("dmr", {})
        c["protocols"]["dmr"]["callHang"] = call_hang
        c["protocols"]["dmr"]["txHang"] = tx_hang
        c["protocols"]["dmr"]["silenceThreshold"] = silence_threshold
        c["protocols"]["dmr"]["frameLossThreshold"] = frame_loss_threshold
        c["protocols"]["dmr"]["queueSize"] = queue_size
        c["protocols"]["dmr"]["repeatDataPacket"] = repeat_data_packet
        c["protocols"]["dmr"]["dumpTAData"] = dump_ta_data
        c["protocols"]["dmr"]["embeddedLCOnly"] = embedded_lc_only
        c["protocols"]["dmr"]["verifyReg"] = verify_reg
        c["protocols"]["dmr"]["ignoreAffiliationCheck"] = ignore_affiliation_check
        c["protocols"]["dmr"]["legacyGroupReg"] = legacy_group_reg
        c["protocols"]["dmr"]["convNetGrantDemand"] = conv_net_grant_demand

        c["protocols"]["dmr"].setdefault("beacons", {})
        c["protocols"]["dmr"]["beacons"]["enable"] = beacon_enable
        c["protocols"]["dmr"]["beacons"]["interval"] = beacon_interval
        c["protocols"]["dmr"]["beacons"]["duration"] = beacon_duration

        c["protocols"]["dmr"].setdefault("control", {})
        c["protocols"]["dmr"]["control"]["enable"] = tscc_enable
        c["protocols"]["dmr"]["control"]["slot"] = tscc_slot
        c["protocols"]["dmr"]["control"]["dedicated"] = tscc_dedicated

        c.setdefault("system", {}).setdefault("config", {})
        c["system"]["config"]["colorCode"] = color_code
        c["system"]["config"]["dmrNetId"] = dmr_net_id
        c["system"]["config"]["siteId"] = site_id
        c["system"]["config"]["defaultNetIdleTalkgroup"] = default_net_idle_tg

        c.setdefault("system", {}).setdefault("modem", {})
        c["system"]["modem"]["dmrRxDelay"] = dmr_rx_delay
        c["system"]["modem"]["txLevel"] = tx_level
        c["system"]["modem"].setdefault("hotspot", {})
        c["system"]["modem"]["hotspot"]["txTuning"] = tx_tuning
        c["system"]["modem"]["hotspot"]["rxTuning"] = rx_tuning
        c["system"]["modem"]["hotspot"]["dmrDiscBWAdj"] = dmr_disc_bw
        c["system"]["modem"]["hotspot"]["dmrPostBWAdj"] = dmr_post_bw
        c["system"]["modem"]["hotspot"]["adfGainMode"] = adf_gain_mode
        c["system"]["modem"]["hotspot"]["afcEnable"] = afc_enable

        # Update frequencies & IDEN table
        dvm_config_manager.set_tuned_frequencies(tx_freq_mhz, rx_freq_mhz, c)

    conf = dvm_config_manager.load_config_dict()
    apply_dmr_fields(conf)

    # Always enforce DMR as active protocol when saving DMR
    conf.setdefault("protocols", {})
    conf["protocols"].setdefault("dmr", {})["enable"] = True
    conf["protocols"].setdefault("p25", {})["enable"] = False

    dvm_config_manager.save_config_dict(conf, summary=f"Update active DMR settings (Color Code {color_code}, TX {tx_freq_mhz} MHz)")

    # Persist directly into DMR profile backup so settings are preserved if switching
    from dvm_nexus.config_engine.profile_manager import profile_manager
    profile_manager.sync_protocol_settings("dmr", apply_dmr_fields)

    log_audit("DMR", "SAVE_CONFIG", f"Updated active DMR parameters: CC={color_code}, NetID={dmr_net_id}, TX={tx_freq_mhz} MHz, RX={rx_freq_mhz} MHz")

    msg = f"✓ DMR settings auto-saved to active config! (TX: {tx_freq_mhz} MHz, CC: {color_code})"

    return render_template(
        request,
        "components/toast.html",
        {"type": "success", "message": msg}
    )


# Talkgroup management endpoints
@router.post("/dmr/talkgroups/add", response_class=HTMLResponse)
async def dmr_tg_add(
    request: Request,
    tgid: int = Form(...),
    name: str = Form(...),
    slot: int = Form(1),
):
    """Add a talkgroup to talkgroup_rules.yml."""
    rules = dvm_config_manager.load_talkgroup_rules()
    for r in rules:
        if r.get("id") == tgid or r.get("tgid") == tgid:
            return render_template(
                request,
                "components/toast.html",
                {"type": "error", "message": f"Talkgroup {tgid} already exists"}
            )

    rules.append({"id": tgid, "name": name, "slot": slot})
    dvm_config_manager.save_talkgroup_rules(rules, summary=f"Add DMR talkgroup {tgid} ({name})")
    log_audit("DMR", "ADD_TG", f"Added TG {tgid} ({name}) Slot {slot}")

    return render_template(
        request,
        "components/dmr_tg_table.html",
        {"talkgroups": rules, "toast": {"type": "success", "message": f"Added Talkgroup {tgid}"}}
    )


@router.post("/dmr/talkgroups/delete/{tgid}", response_class=HTMLResponse)
async def dmr_tg_delete(request: Request, tgid: int):
    """Delete a talkgroup from talkgroup_rules.yml."""
    rules = dvm_config_manager.load_talkgroup_rules()
    filtered = [r for r in rules if r.get("id") != tgid and r.get("tgid") != tgid]
    dvm_config_manager.save_talkgroup_rules(filtered, summary=f"Delete DMR talkgroup {tgid}")
    log_audit("DMR", "DEL_TG", f"Deleted TG {tgid}")

    return render_template(
        request,
        "components/dmr_tg_table.html",
        {"talkgroups": filtered, "toast": {"type": "success", "message": f"Deleted Talkgroup {tgid}"}}
    )


# Radio ID ACL endpoints
@router.post("/dmr/rids/add", response_class=HTMLResponse)
async def dmr_rid_add(
    request: Request,
    rid: int = Form(...),
    alias: str = Form(""),
    enabled: bool = Form(True),
):
    """Add a Radio ID to rid_acl.dat."""
    rids = dvm_config_manager.load_rid_acl()
    for item in rids:
        if item.get("rid") == rid:
            return render_template(
                request,
                "components/toast.html",
                {"type": "error", "message": f"Radio ID {rid} already exists"}
            )

    rids.append({"rid": rid, "alias": alias, "enabled": enabled, "ip": ""})
    dvm_config_manager.save_rid_acl(rids, summary=f"Add Radio ID {rid} ({alias})")
    log_audit("DMR", "ADD_RID", f"Added RID {rid} ({alias})")

    return render_template(
        request,
        "components/dmr_rid_table.html",
        {"rids": rids, "toast": {"type": "success", "message": f"Added Radio ID {rid}"}}
    )


@router.post("/dmr/rids/delete/{rid}", response_class=HTMLResponse)
async def dmr_rid_delete(request: Request, rid: int):
    """Delete a Radio ID from rid_acl.dat."""
    rids = dvm_config_manager.load_rid_acl()
    filtered = [item for item in rids if item.get("rid") != rid]
    dvm_config_manager.save_rid_acl(filtered, summary=f"Delete Radio ID {rid}")
    log_audit("DMR", "DEL_RID", f"Deleted RID {rid}")

    return render_template(
        request,
        "components/dmr_rid_table.html",
        {"rids": filtered, "toast": {"type": "success", "message": f"Deleted Radio ID {rid}"}}
    )


@router.post("/dmr/rids/action", response_class=HTMLResponse)
async def dmr_rid_action(
    request: Request,
    action: str = Form(...),
    rid: int = Form(...),
    slot: int = Form(1),
):
    """Dispatch over-the-air DMR RID command (page, check, inhibit, uninhibit)."""
    res = dvm_client.dmr_rid_action(action=action, rid=rid, slot=slot)
    success = res.get("success", False)
    msg = f"DMR {action.upper()} to RID {rid}: {'Success' if success else res.get('error', 'Failed')}"
    log_audit("DMR_OTA", action.upper(), f"Target RID: {rid}, Slot: {slot}, Result: {msg}")

    return render_template(
        request,
        "components/toast.html",
        {"type": "success" if success else "error", "message": msg}
    )


@router.post("/dmr/send-sms", response_class=HTMLResponse)
async def dmr_send_sms(
    request: Request,
    target_id: int = Form(...),
    target_type: str = Form("group"),
    text: str = Form(...),
    forward_to_mesh: Optional[str] = Form("1"),
):
    """Send DMR Text Message (TMS/SMS) to Talkgroup or Radio ID, optionally forwarding to Meshtastic."""
    is_group = target_type == "group"
    from dvm_nexus.meshtastic.text_gateway import text_gateway
    res = text_gateway.send_text_from_web(
        protocol="dmr",
        dest_id=target_id,
        is_group=is_group,
        text=text,
        forward_to_mesh=True if forward_to_mesh == "1" else False
    )
    success = res.get("success", False)
    msg = f"DMR TMS sent to {'TG' if is_group else 'RID'} {target_id}: '{text}'" + (" (Mirrored to Mesh)" if forward_to_mesh == "1" else "")
    return render_template(
        request,
        "components/toast.html",
        {"type": "success" if success else "error", "message": msg}
    )
