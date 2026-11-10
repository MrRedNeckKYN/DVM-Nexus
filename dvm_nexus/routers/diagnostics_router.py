"""
DVM Nexus - Diagnostics & Live Service Logs Router
Provides live health checks, crash log streaming, and 1-click automated repair for DVMHost.
"""

from fastapi import APIRouter, Request, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from typing import Optional

from dvm_nexus.core.templates import render_template
from dvm_nexus.core.diagnostics import diagnostics_engine
from dvm_nexus.core.service_controller import service_controller

router = APIRouter()


@router.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request):
    """Render the full Diagnostics, Health Doctor, and Live Logs page."""
    health = diagnostics_engine.run_health_checks()
    logs_data = diagnostics_engine.get_service_logs(service_name="dvmhost", lines=80, filter_level="all")
    service_status = service_controller.get_status()

    return render_template(
        request,
        "diagnostics.html",
        {
            "title": "Diagnostics & Live Logs",
            "active_tab": "diagnostics",
            "health": health,
            "logs": logs_data,
            "service": service_status,
        }
    )


@router.get("/components/diagnostics-logs", response_class=HTMLResponse)
async def diagnostics_logs_component(
    request: Request,
    service: str = Query("dvmhost"),
    lines: int = Query(80),
    filter: str = Query("all")
):
    """HTMX polling/filtering fragment for terminal log viewer."""
    logs_data = diagnostics_engine.get_service_logs(service_name=service, lines=lines, filter_level=filter)
    return render_template(
        request,
        "components/diagnostics_logs.html",
        {
            "logs": logs_data,
            "selected_service": service,
            "selected_filter": filter,
        }
    )


@router.get("/components/diagnostics-checks", response_class=HTMLResponse)
async def diagnostics_checks_component(request: Request):
    """HTMX fragment for Health Doctor checklist."""
    health = diagnostics_engine.run_health_checks()
    return render_template(
        request,
        "components/diagnostics_checks.html",
        {"health": health}
    )


@router.post("/components/diagnostics-autofix", response_class=HTMLResponse)
async def diagnostics_autofix_component(request: Request):
    """HTMX action to execute 1-Click Auto-Repair on all detected configuration issues."""
    result = diagnostics_engine.auto_fix_all()
    health = diagnostics_engine.run_health_checks()
    service_status = service_controller.get_status()
    logs_data = diagnostics_engine.get_service_logs(service_name="dvmhost", lines=80, filter_level="all")

    return render_template(
        request,
        "components/diagnostics_results.html",
        {
            "result": result,
            "health": health,
            "service": service_status,
            "logs": logs_data,
        }
    )


@router.get("/api/diagnostics/status")
async def api_diagnostics_status():
    """JSON API returning full automated diagnostic report."""
    health = diagnostics_engine.run_health_checks()
    return JSONResponse(health)


@router.get("/api/diagnostics/logs")
async def api_diagnostics_logs(
    service: str = Query("dvmhost"),
    lines: int = Query(100),
    filter: str = Query("all")
):
    """JSON API returning raw and parsed service journalctl logs."""
    logs_data = diagnostics_engine.get_service_logs(service_name=service, lines=lines, filter_level=filter)
    return JSONResponse(logs_data)


@router.get("/api/diagnostics/export", response_class=PlainTextResponse)
async def api_diagnostics_export(service: str = Query("dvmhost"), lines: int = Query(200)):
    """Export raw journal logs as downloadable plain text."""
    logs_data = diagnostics_engine.get_service_logs(service_name=service, lines=lines, filter_level="all")
    content = "\n".join(e["raw"] for e in logs_data["entries"])
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f"attachment; filename={service}_journal.log"}
    )
