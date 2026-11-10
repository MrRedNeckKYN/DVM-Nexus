"""
DVM Nexus - Network Builder Wizard Router
Guides the user through creating complete DMR or P25 networks with automated configuration generation.
"""

import yaml
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
from typing import Optional, List, Dict, Any

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.iden_calculator import (
    BAND_PRESETS,
    read_iden_table_file,
    format_iden_table,
    find_or_create_iden_entry,
)
from dvm_nexus.config_engine.network_validator import validate_dmr_settings, validate_p25_settings
from dvm_nexus.config_engine.diff_engine import generate_unified_diff, generate_structured_diff
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.profile_manager import profile_manager
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.database import log_audit

router = APIRouter()


@router.get("/wizard", response_class=HTMLResponse)
async def wizard_page(request: Request):
    """Render the Network Builder wizard."""
    return render_template(
        request,
        "wizard.html",
        {
            "title": "Network Builder Wizard",
            "active_tab": "wizard",
            "band_presets": BAND_PRESETS,
        }
    )


@router.post("/wizard/preview", response_class=HTMLResponse)
async def wizard_preview(
    request: Request,
    protocol: str = Form(...),
    network_name: str = Form(...),
    identity: str = Form("ABCD123"),
    duplex: bool = Form(True),
    band_preset: str = Form("uhf_440"),
    tx_freq_mhz: float = Form(446.000),
    rx_offset_mhz: float = Form(5.000),
    spacing_khz: float = Form(6.25),
    # DMR specific
    color_code: int = Form(1),
    dmr_net_id: int = Form(1),
    # P25 specific
    nac: str = Form("293"),
    wacn: str = Form("BB800"),
    sys_id: str = Form("001"),
    rfss_id: int = Form(1),
    site_id: int = Form(1),
    # Initial TG & RID
    talkgroups_raw: str = Form("1:Local:1\n9:Worldwide:2"),
    rids_raw: str = Form("1234567:Primary Radio"),
    # Upstream peer
    connect_peer: bool = Form(False),
    peer_id: int = Form(100000),
    peer_address: str = Form("127.0.0.1"),
    peer_port: int = Form(62031),
    peer_password: str = Form("PASSWORD"),
):
    """Generate configuration preview, calculate IDEN channels, and produce diff."""
    errors: List[str] = []

    # Validation
    if protocol == "dmr":
        dmr_errors = validate_dmr_settings({"colorCode": color_code, "dmrNetId": dmr_net_id, "siteId": site_id})
        errors.extend(dmr_errors)
    elif protocol == "p25":
        p25_errors = validate_p25_settings({"nac": nac, "netId": wacn, "sysId": sys_id, "rfssId": rfss_id, "siteId": site_id})
        errors.extend(p25_errors)

    if errors:
        return render_template(
            request,
            "components/wizard_error.html",
            {"errors": errors}
        )

    # Calculate frequencies and IDEN mapping
    tx_freq_hz = int(tx_freq_mhz * 1000000)
    rx_freq_hz = int((tx_freq_mhz + rx_offset_mhz) * 1000000)

    iden_entries = read_iden_table_file(dvm_config_manager.iden_path)
    entry, channel_no = find_or_create_iden_entry(
        entries=iden_entries,
        tx_freq_hz=tx_freq_hz,
        rx_freq_hz=rx_freq_hz,
        spacing_khz=spacing_khz,
    )
    channel_id = entry.channel_id

    # Parse initial talkgroups
    parsed_tgs = []
    for line in talkgroups_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":") if p.strip()]
        if len(parts) >= 2:
            try:
                tgid = int(parts[0])
                name = parts[1]
                slot = int(parts[2]) if len(parts) > 2 else 1
                parsed_tgs.append({"id": tgid, "name": name, "slot": slot})
            except ValueError:
                pass

    # Parse initial RIDs
    parsed_rids = []
    for line in rids_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":") if p.strip()]
        if len(parts) >= 1:
            try:
                rid = int(parts[0])
                alias = parts[1] if len(parts) > 1 else ""
                parsed_rids.append({"rid": rid, "enabled": True, "alias": alias})
            except ValueError:
                pass

    # Build new configuration dictionary
    new_conf = dvm_config_manager.load_config_dict()
    new_conf.setdefault("system", {})
    new_conf["system"]["identity"] = identity
    new_conf["system"]["duplex"] = duplex

    new_conf.setdefault("system", {}).setdefault("config", {})
    new_conf["system"]["config"]["channelId"] = channel_id
    new_conf["system"]["config"]["channelNo"] = channel_no
    new_conf["system"]["config"]["siteId"] = site_id

    new_conf.setdefault("protocols", {})
    if protocol == "dmr":
        new_conf["protocols"]["dmr"] = {"enable": True}
        new_conf["protocols"]["p25"] = {"enable": False}
        new_conf["system"]["config"]["colorCode"] = color_code
        new_conf["system"]["config"]["dmrNetId"] = dmr_net_id
    else:
        new_conf["protocols"]["dmr"] = {"enable": False}
        new_conf["protocols"]["p25"] = {"enable": True}
        new_conf["system"]["config"]["nac"] = nac
        new_conf["system"]["config"]["netId"] = wacn
        new_conf["system"]["config"]["sysId"] = sys_id
        new_conf["system"]["config"]["rfssId"] = rfss_id

    # Network peer settings
    new_conf.setdefault("network", {})
    new_conf["network"]["enable"] = connect_peer
    if connect_peer:
        new_conf["network"]["id"] = peer_id
        new_conf["network"]["address"] = peer_address
        new_conf["network"]["port"] = peer_port
        new_conf["network"]["password"] = peer_password

    # Generate texts for diff
    current_config_yaml = dvm_config_manager.load_config_raw()
    proposed_config_yaml = yaml.dump(new_conf, default_flow_style=False, sort_keys=False)
    diff_text = generate_unified_diff(current_config_yaml, proposed_config_yaml)
    structured_diff = generate_structured_diff(current_config_yaml, proposed_config_yaml)

    proposed_iden_table = format_iden_table(iden_entries)

    return render_template(
        request,
        "components/wizard_preview.html",
        {
            "network_name": network_name,
            "protocol": protocol,
            "channel_id": channel_id,
            "channel_no": channel_no,
            "tx_freq_mhz": tx_freq_mhz,
            "rx_freq_mhz": round(rx_freq_hz / 1000000.0, 5),
            "talkgroups": parsed_tgs,
            "rids": parsed_rids,
            "proposed_yaml": proposed_config_yaml,
            "proposed_iden": proposed_iden_table,
            "diff_text": diff_text,
            "structured_diff": structured_diff,
        }
    )


@router.post("/wizard/apply", response_class=HTMLResponse)
async def wizard_apply(
    request: Request,
    network_name: str = Form(...),
    protocol: str = Form(...),
    proposed_yaml: str = Form(...),
    proposed_iden: str = Form(...),
    talkgroups_json: str = Form("[]"),
    rids_json: str = Form("[]"),
    save_as_profile: bool = Form(True),
):
    """Safely apply wizard-generated configuration."""
    import json
    # Backup current state
    backup_dir = backup_manager.create_backup(summary=f"Wizard creation: {network_name} ({protocol.upper()})")

    try:
        # Write files
        backup_manager.atomic_write(dvm_config_manager.config_path, proposed_yaml)
        backup_manager.atomic_write(dvm_config_manager.iden_path, proposed_iden)

        # Write talkgroups if provided
        tgs = json.loads(talkgroups_json)
        if tgs:
            dvm_config_manager.save_talkgroup_rules(tgs, summary=f"Wizard TG setup for {network_name}")

        # Write RIDs if provided
        rids = json.loads(rids_json)
        if rids:
            dvm_config_manager.save_rid_acl(rids, summary=f"Wizard RID setup for {network_name}")

        # Save as a distinct profile
        if save_as_profile:
            prof_id = profile_manager.save_current_as_profile(
                name=network_name,
                protocol=protocol,
                description=f"Generated by Network Builder Wizard for {network_name}"
            )
            from dvm_nexus.core.database import get_db_connection
            conn = get_db_connection()
            try:
                with conn:
                    conn.execute("UPDATE profiles SET is_active = 0")
                    conn.execute("UPDATE profiles SET is_active = 1 WHERE id = ?", (prof_id,))
            finally:
                conn.close()

        # Restart service safely and verify
        service_controller.restart()
        verified = service_controller.verify_running(timeout_sec=4.0)

        log_audit("WIZARD", "APPLY", f"Applied new network '{network_name}' ({protocol.upper()})")

        return render_template(
            request,
            "components/wizard_complete.html",
            {
                "network_name": network_name,
                "protocol": protocol,
                "verified": verified,
                "backup_dir": str(backup_dir),
            }
        )
    except Exception as e:
        # Rollback on failure
        backup_manager.rollback(revision_id=1)
        log_audit("WIZARD", "APPLY_ERROR", f"Failed applying wizard: {e}", status="error")
        return render_template(
            request,
            "components/wizard_error.html",
            {"errors": [f"Failed to apply configuration: {e}. Configuration was rolled back."]}
        )
