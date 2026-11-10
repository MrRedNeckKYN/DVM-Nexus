"""
DVM Nexus - Network & Wi-Fi Management Router
Handles Wi-Fi Client scanning, connection, and Failover Field Hotspot management via NetworkManager (nmcli).
"""

from fastapi import APIRouter, Request, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List

from dvm_nexus.core.templates import render_template
from dvm_nexus.core.network_manager import network_controller
from dvm_nexus.core.database import log_audit

router = APIRouter()


class HotspotTogglePayload(BaseModel):
    enable: bool = True


class NetworkConnectPayload(BaseModel):
    ssid: str
    password: Optional[str] = ""


# ==============================================================================
# HTML Web UI Endpoints
# ==============================================================================

@router.get("/network", response_class=HTMLResponse)
async def network_page(request: Request):
    """Render the full Network & Wi-Fi Management dashboard page."""
    net_status = network_controller.get_status()
    networks = network_controller.scan_networks()

    return render_template(
        request,
        "network.html",
        {
            "title": "Wi-Fi & Field Hotspot",
            "active_tab": "network",
            "net": net_status,
            "networks": networks,
        }
    )


@router.get("/components/network-status", response_class=HTMLResponse)
async def network_status_component(request: Request):
    """HTMX polling fragment for Live Status Hero Card."""
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_status.html",
        {"net": net_status}
    )


@router.get("/components/network-scan", response_class=HTMLResponse)
async def network_scan_component(request: Request):
    """HTMX fragment for triggering a live Wi-Fi scan."""
    networks = network_controller.scan_networks()
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_scan.html",
        {"networks": networks, "net": net_status}
    )


@router.post("/components/network-toggle", response_class=HTMLResponse)
async def network_toggle_component(request: Request, enable: Optional[str] = Form("false")):
    """HTMX action to toggle between Field Hotspot and Client Wi-Fi."""
    is_enable = enable.lower() in ("true", "1", "yes", "on")
    res = network_controller.toggle_hotspot(is_enable)
    log_audit("NETWORK", "HOTSPOT_TOGGLE", f"Hotspot {'enabled' if is_enable else 'disabled'}: {res.get('message', '')}")
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_status.html",
        {"net": net_status, "flash_message": res.get("message") or res.get("error"), "flash_type": "success" if res.get("success") else "danger"}
    )


@router.post("/components/network-connect", response_class=HTMLResponse)
async def network_connect_component(
    request: Request,
    ssid: str = Form(...),
    password: Optional[str] = Form(None)
):
    """HTMX action to connect to a client Wi-Fi network."""
    res = network_controller.connect_network(ssid, password)
    log_audit("NETWORK", "CONNECT", f"Wi-Fi connect attempt to '{ssid}': success={res.get('success')}")
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_status.html",
        {
            "net": net_status,
            "flash_message": res.get("message") or res.get("error"),
            "flash_type": "success" if res.get("success") else "danger"
        }
    )


# ==============================================================================
# REST API Endpoints (Part 2 Specification)
# ==============================================================================

@router.get("/api/network/status")
async def api_network_status():
    """
    GET /api/network/status
    Returns current network mode ("client" or "hotspot") and interface details.
    """
    status = network_controller.get_status()
    return JSONResponse(status)


@router.post("/api/network/hotspot/toggle")
async def api_hotspot_toggle(payload: HotspotTogglePayload):
    """
    POST /api/network/hotspot/toggle
    Payload: { "enable": true | false }
    Activates or deactivates the Field-Hotspot profile.
    """
    enable = payload.enable
    res = network_controller.toggle_hotspot(enable)
    log_audit("API", "HOTSPOT_TOGGLE", f"API Hotspot toggle: enable={enable}")
    status_code = 200 if res.get("success") else res.get("status_code", 500)
    return JSONResponse(res, status_code=status_code)


@router.get("/api/network/scan")
async def api_network_scan():
    """
    GET /api/network/scan
    Scans for Wi-Fi networks and returns sanitized, deduplicated, sorted JSON list.
    """
    networks = network_controller.scan_networks()
    return JSONResponse({"count": len(networks), "networks": networks})


@router.post("/api/network/connect")
async def api_network_connect(payload: NetworkConnectPayload):
    """
    POST /api/network/connect
    Payload: { "ssid": "...", "password": "..." }
    Connects to specified Wi-Fi network and sets autoconnect-priority to 100.
    """
    ssid = payload.ssid.strip()
    password = payload.password

    if not ssid:
        return JSONResponse({"success": False, "error": "Missing required field 'ssid'"}, status_code=400)

    res = network_controller.connect_network(ssid, password)
    log_audit("API", "WIFI_CONNECT", f"API Wi-Fi connect attempt to '{ssid}': success={res.get('success')}")
    status_code = 200 if res.get("success") else res.get("status_code", 500)
    return JSONResponse(res, status_code=status_code)
