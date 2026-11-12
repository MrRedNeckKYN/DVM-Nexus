"""
DVM Nexus - System Auto-Updater Router
Web interface for checking GitHub releases, viewing changelogs, and triggering in-place 1-click updates.
"""

import asyncio
import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional

from dvm_nexus.core.templates import render_template
from dvm_nexus.core.updater import system_updater
from dvm_nexus.core.dvmhost_updater import dvmhost_updater

router = APIRouter(prefix="/updates", tags=["updates"])


@router.get("", response_class=HTMLResponse)
async def updates_dashboard(request: Request):
    """Render the System Updates management page (auto-checking GitHub if cache is stale)."""
    if not os.environ.get("PYTEST_CURRENT_TEST") and system_updater.should_auto_check():
        await asyncio.to_thread(system_updater.check_for_updates)
    status = await asyncio.to_thread(system_updater.get_status)
    dvmhost_status = await asyncio.to_thread(dvmhost_updater.get_status)
    return render_template(
        request,
        "updates.html",
        {
            "title": "System Updates",
            "active_tab": "updates",
            "updater": status,
            "dvmhost_updater": dvmhost_status,
        }
    )


@router.post("/check", response_class=HTMLResponse)
async def check_updates(request: Request, channel: Optional[str] = Form("main")):
    """Check GitHub repository for newer commits or releases without blocking the event loop."""
    await asyncio.to_thread(system_updater.check_for_updates, channel=channel)
    status = await asyncio.to_thread(system_updater.get_status)
    return render_template(
        request,
        "components/update_card.html",
        {"updater": status, "oob_sync": True}
    )


@router.post("/apply", response_class=HTMLResponse)
async def apply_update(request: Request, channel: Optional[str] = Form("main")):
    """Trigger in-place auto update without blocking the event loop."""
    res = await asyncio.to_thread(system_updater.apply_update, channel=channel)
    status = await asyncio.to_thread(system_updater.get_status)
    return render_template(
        request,
        "components/update_progress.html",
        {"res": res, "updater": status, "oob_sync": True}
    )


@router.post("/dvmhost/check", response_class=HTMLResponse)
async def check_dvmhost_updates(request: Request):
    """Check official DVMProject/dvmhost releases for newer binaries."""
    await asyncio.to_thread(dvmhost_updater.check_for_updates)
    status = await asyncio.to_thread(dvmhost_updater.get_status)
    return render_template(
        request,
        "components/dvmhost_update_card.html",
        {"dvmhost_updater": status}
    )


@router.post("/dvmhost/apply", response_class=HTMLResponse)
async def apply_dvmhost_update(request: Request, download_url: Optional[str] = Form(None)):
    """Apply DVMHost binary upgrade with safety snapshot and verification."""
    res = await asyncio.to_thread(dvmhost_updater.apply_update, download_url=download_url)
    status = await asyncio.to_thread(dvmhost_updater.get_status)
    return render_template(
        request,
        "components/dvmhost_update_progress.html",
        {"res": res, "dvmhost_updater": status}
    )


@router.get("/components/status", response_class=HTMLResponse)
async def get_status_partial(request: Request):
    status = await asyncio.to_thread(system_updater.get_status)
    return render_template(
        request,
        "components/update_card.html",
        {"updater": status}
    )


@router.get("/components/dvmhost-status", response_class=HTMLResponse)
async def get_dvmhost_status_partial(request: Request):
    status = await asyncio.to_thread(dvmhost_updater.get_status)
    return render_template(
        request,
        "components/dvmhost_update_card.html",
        {"dvmhost_updater": status}
    )


@router.get("/api/status", response_class=JSONResponse)
async def api_status():
    nexus_status = await asyncio.to_thread(system_updater.get_status)
    dvm_status = await asyncio.to_thread(dvmhost_updater.get_status)
    return {
        "nexus": nexus_status,
        "dvmhost": dvm_status
    }
