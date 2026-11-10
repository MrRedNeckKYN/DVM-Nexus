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
    """Render the DMR management interface."""
    conf = dvm_config_manager.load_config_dict()
    dmr_conf = conf.get("protocols", {}).get("dmr", {})
    sys_conf = conf.get("system", {}).get("config", {})
    modem_conf = conf.get("system", {}).get("modem", {})
    hotspot_conf = modem_conf.get("hotspot", {})
    net_conf = conf.get("network", {})

    talkgroups = dvm_config_manager.load_talkgroup_rules()
    rids = dvm_config_manager.load_rid_acl()

    return render_template(
        request,
        "dmr.html",
        {
            "title": "DMR Management",
            "active_tab": "dmr",
            "dmr": dmr_conf,
            "sys": sys_conf,
            "modem": modem_conf,
            "hotspot": hotspot_conf,
            "net": net_conf,
            "talkgroups": talkgroups,
            "rids": rids,
        }
    )


@router.post("/dmr/save", response_class=HTMLResponse)
async def dmr_save(
    request: Request,
    enable: bool = Form(True),
    color_code: int = Form(1),
    dmr_net_id: int = Form(1),
    site_id: int = Form(1),
    slot1: bool = Form(True),
    slot2: bool = Form(True),
    call_hang: int = Form(5),
    tx_hang: int = Form(8),
    beacon_enable: bool = Form(False),
    beacon_interval: int = Form(60),
    beacon_duration: int = Form(3),
    dmr_rx_delay: int = Form(7),
    dmr_disc_bw: int = Form(0),
    dmr_post_bw: int = Form(0),
):
    """Save DMR parameters to config.yml safely."""
    errors = validate_dmr_settings({"colorCode": color_code, "dmrNetId": dmr_net_id, "siteId": site_id})
    if errors:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "; ".join(errors)}
        )

    conf = dvm_config_manager.load_config_dict()
    conf.setdefault("protocols", {}).setdefault("dmr", {})
    conf["protocols"]["dmr"]["enable"] = enable
    conf["protocols"]["dmr"]["callHang"] = call_hang
    conf["protocols"]["dmr"]["txHang"] = tx_hang

    conf["protocols"]["dmr"].setdefault("beacons", {})
    conf["protocols"]["dmr"]["beacons"]["enable"] = beacon_enable
    conf["protocols"]["dmr"]["beacons"]["interval"] = beacon_interval
    conf["protocols"]["dmr"]["beacons"]["duration"] = beacon_duration

    conf.setdefault("system", {}).setdefault("config", {})
    conf["system"]["config"]["colorCode"] = color_code
    conf["system"]["config"]["dmrNetId"] = dmr_net_id
    conf["system"]["config"]["siteId"] = site_id

    conf.setdefault("network", {})
    conf["network"]["slot1"] = slot1
    conf["network"]["slot2"] = slot2

    conf.setdefault("system", {}).setdefault("modem", {})
    conf["system"]["modem"]["dmrRxDelay"] = dmr_rx_delay
    conf["system"]["modem"].setdefault("hotspot", {})
    conf["system"]["modem"]["hotspot"]["dmrDiscBWAdj"] = dmr_disc_bw
    conf["system"]["modem"]["hotspot"]["dmrPostBWAdj"] = dmr_post_bw

    dvm_config_manager.save_config_dict(conf, summary=f"Update DMR settings (Color Code {color_code})")
    log_audit("DMR", "SAVE_CONFIG", f"Updated DMR parameters: CC={color_code}, NetID={dmr_net_id}")

    return render_template(
        request,
        "components/toast.html",
        {"type": "success", "message": "DMR settings saved successfully! Restart service to apply."}
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
