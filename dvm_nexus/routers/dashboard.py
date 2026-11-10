"""
DVM Nexus - Dashboard Router
Provides overview stats, service controls, and reactive HTMX widgets.
"""

import asyncio
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from dvm_nexus.core.templates import render_template
from dvm_nexus.core.system_monitor import get_system_metrics
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.dvm_client import dvm_client
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.profile_manager import profile_manager
from dvm_nexus.core.database import get_recent_audit_logs
from dvm_nexus.core.diagnostics import diagnostics_engine

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Render the main router-style dashboard."""
    sys_metrics = get_system_metrics()
    service_status = service_controller.get_status()
    dvm_status = dvm_config_manager.get_quick_status()
    active_profile = profile_manager.get_active_profile()
    recent_logs = get_recent_audit_logs(limit=10)
    health = diagnostics_engine.run_health_checks()

    # Attempt live query from DVMHost REST API
    live_rest = None
    if service_status["is_running"] and dvm_status.get("rest_enabled"):
        live_query = dvm_client.get_status()
        if live_query.get("success"):
            live_rest = live_query.get("data")

    return render_template(
        request,
        "dashboard.html",
        {
            "title": "Dashboard",
            "active_tab": "dashboard",
            "sys": sys_metrics,
            "service": service_status,
            "dvm": dvm_status,
            "profile": active_profile,
            "logs": recent_logs,
            "live_rest": live_rest,
            "health": health,
        }
    )


@router.get("/components/system-stats", response_class=HTMLResponse)
async def system_stats_component(request: Request):
    """HTMX polling fragment for CPU, RAM, Disk, and Temperature."""
    sys_metrics = get_system_metrics()
    service_status = service_controller.get_status()
    return render_template(
        request,
        "components/sys_stats.html",
        {"sys": sys_metrics, "service": service_status}
    )


@router.get("/components/service-badge", response_class=HTMLResponse)
async def service_badge_component(request: Request):
    """HTMX polling fragment for service status badge."""
    service_status = service_controller.get_status()
    return render_template(
        request,
        "components/status_badge.html",
        {"service": service_status}
    )


@router.post("/api/service/start", response_class=HTMLResponse)
async def service_start(request: Request):
    """Start dvmhost service."""
    res = await asyncio.to_thread(service_controller.start)
    service_status = service_controller.get_status()
    return render_template(
        request,
        "components/service_controls.html",
        {
            "service": service_status,
            "message": res.get("message"),
            "is_success": res.get("success"),
            "recent_logs": res.get("recent_logs", []),
        }
    )


@router.post("/api/service/stop", response_class=HTMLResponse)
async def service_stop(request: Request):
    """Stop dvmhost service."""
    res = await asyncio.to_thread(service_controller.stop)
    service_status = service_controller.get_status()
    return render_template(
        request,
        "components/service_controls.html",
        {
            "service": service_status,
            "message": res.get("message"),
            "is_success": res.get("success"),
            "recent_logs": res.get("recent_logs", []),
        }
    )


@router.post("/api/service/restart", response_class=HTMLResponse)
async def service_restart(request: Request):
    """Restart dvmhost service."""
    res = await asyncio.to_thread(service_controller.restart)
    service_status = service_controller.get_status()
    return render_template(
        request,
        "components/service_controls.html",
        {
            "service": service_status,
            "message": res.get("message"),
            "is_success": res.get("success"),
            "recent_logs": res.get("recent_logs", []),
        }
    )
