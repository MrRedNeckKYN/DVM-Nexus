"""
DVM Nexus - Configuration History, Diffs, and Raw Editor Router
"""

import asyncio
import yaml
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path
from typing import Optional, List, Dict, Any

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.backup_manager import backup_manager
from dvm_nexus.config_engine.diff_engine import generate_unified_diff, generate_structured_diff
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.network_validator import validate_raw_config, validate_port
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.database import log_audit

router = APIRouter()


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Render Configuration Revisions, Diff, REST API Settings, and Raw Editor page."""
    revisions = backup_manager.list_revisions(limit=30)
    raw_yaml = dvm_config_manager.load_config_raw()
    conf = dvm_config_manager.load_config_dict()
    net = conf.get("network", {}) if isinstance(conf.get("network"), dict) else {}

    rest_settings = {
        "rest_enable": net.get("restEnable", True),
        "rest_port": net.get("restPort", 9990),
        "rest_address": net.get("restAddress", "127.0.0.1"),
        "rest_password": net.get("restPassword", "PASSWORD"),
        "rpc_port": net.get("rpcPort", 9890),
        "rpc_password": net.get("rpcPassword", "ULTRA-VERY-SECURE-DEFAULT"),
    }

    return render_template(
        request,
        "config.html",
        {
            "title": "Configuration & History",
            "active_tab": "config",
            "revisions": revisions,
            "raw_yaml": raw_yaml,
            "rest": rest_settings,
        }
    )


@router.post("/config/api-settings/save", response_class=HTMLResponse)
async def config_api_settings_save(
    request: Request,
    rest_enable: Optional[str] = Form(None),
    rest_port: int = Form(9990),
    rest_address: str = Form("127.0.0.1"),
    rest_password: str = Form("PASSWORD"),
    rpc_port: int = Form(9890),
    rpc_password: str = Form("ULTRA-VERY-SECURE-DEFAULT"),
):
    """Save DVMHost REST and RPC API connection settings directly to active config."""
    port_errors = validate_port("REST port", rest_port) + validate_port("RPC port", rpc_port)
    if port_errors:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Not saved - " + "; ".join(port_errors)}
        )
    conf = dvm_config_manager.load_config_dict()
    if "network" not in conf or not isinstance(conf["network"], dict):
        conf["network"] = {}

    enable_val = (rest_enable == "true" or rest_enable == "on" or rest_enable == "1")
    conf["network"]["restEnable"] = enable_val
    conf["network"]["restPort"] = int(rest_port)
    conf["network"]["restAddress"] = rest_address.strip()
    conf["network"]["restPassword"] = rest_password.strip()
    conf["network"]["rpcPort"] = int(rpc_port)
    conf["network"]["rpcPassword"] = rpc_password.strip()

    dvm_config_manager.save_config_dict(conf, summary="Update DVMHost REST/RPC API settings")
    log_audit("CONFIG", "REST_API_UPDATE", f"restEnable={enable_val}, restPort={rest_port}")

    # Restart DVMHost so changes take effect
    await asyncio.to_thread(service_controller.restart)
    running = await asyncio.to_thread(service_controller.verify_running, 4.0)

    msg = f"DVMHost REST API updated (Port: {rest_port}, Enabled: {enable_val}). Daemon restarted (Active: {running})."
    return render_template(
        request,
        "components/toast.html",
        {"type": "success" if running else "warning", "message": msg}
    )


@router.get("/config/diff/{revision_id}", response_class=HTMLResponse)
async def config_diff_view(request: Request, revision_id: int):
    """View visual diff between active config.yml and selected revision."""
    from dvm_nexus.core.database import get_db_connection
    conn = get_db_connection()
    backup_dir = None
    summary = ""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT backup_dir, summary FROM config_revisions WHERE id = ?", (revision_id,))
        row = cursor.fetchone()
        if row:
            backup_dir = row["backup_dir"]
            summary = row["summary"]
    finally:
        conn.close()

    if not backup_dir or not Path(backup_dir).exists():
        return HTMLResponse("<div class='alert alert-danger'>Revision backup folder not found on disk.</div>")

    backup_file = Path(backup_dir) / "config.yml"
    if not backup_file.exists():
        return HTMLResponse("<div class='alert alert-warning'>config.yml was not captured in this revision snapshot.</div>")

    old_text = backup_file.read_text(encoding="utf-8")
    current_text = dvm_config_manager.load_config_raw()

    diff_lines = generate_structured_diff(old_text, current_text)

    return render_template(
        request,
        "components/diff_viewer.html",
        {
            "revision_id": revision_id,
            "summary": summary,
            "diff_lines": diff_lines,
        }
    )


@router.post("/config/rollback/{revision_id}", response_class=HTMLResponse)
async def config_rollback(request: Request, revision_id: int):
    """Rollback to selected revision safely in background thread."""
    res = await asyncio.to_thread(backup_manager.rollback, revision_id)
    revisions = backup_manager.list_revisions(limit=30)
    if res.get("success"):
        await asyncio.to_thread(service_controller.restart)
        verified = await asyncio.to_thread(service_controller.verify_running, 4.0)
        return render_template(
            request,
            "components/revision_table.html",
            {
                "revisions": revisions,
                "toast": {"type": "success", "message": f"Rolled back to revision #{revision_id}! (Service running: {verified})"}
            }
        )
    else:
        return render_template(
            request,
            "components/revision_table.html",
            {
                "revisions": revisions,
                "toast": {"type": "error", "message": res.get("error")}
            }
        )


@router.post("/config/raw-save", response_class=HTMLResponse)
async def config_raw_save(request: Request, raw_content: str = Form(...)):
    """Validate (syntax + structure) and atomically save raw YAML with backup."""
    try:
        parsed = yaml.safe_load(raw_content)
    except yaml.YAMLError as exc:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": f"YAML Syntax Error: {exc}"}
        )

    problems = validate_raw_config(parsed)
    if problems:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Not saved - " + "; ".join(problems)}
        )

    backup_manager.create_backup(summary="Manual raw YAML edit", changed_files=["config.yml"])
    backup_manager.atomic_write(dvm_config_manager.config_path, raw_content)
    log_audit("CONFIG", "RAW_EDIT", "Saved manual raw YAML changes")

    return render_template(
        request,
        "components/toast.html",
        {"type": "success", "message": "Raw configuration saved safely! Restart service to apply."}
    )
