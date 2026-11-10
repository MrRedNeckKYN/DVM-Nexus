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


class HotspotConfigPayload(BaseModel):
    ssid: Optional[str] = None
    password: Optional[str] = None
    band: Optional[str] = None
    channel: Optional[int] = None
    auto_failover_enabled: Optional[bool] = None
    failover_threshold_sec: Optional[int] = None


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


@router.post("/components/network-hotspot-config", response_class=HTMLResponse)
async def network_hotspot_config_component(
    request: Request,
    ssid: str = Form(...),
    password: str = Form(...),
    band: str = Form("bg"),
    channel: int = Form(6),
    failover_threshold_sec: int = Form(45),
    auto_failover_enabled: Optional[str] = Form("false"),
):
    """HTMX action to save hotspot settings and provision directly to NetworkManager."""
    is_auto = auto_failover_enabled.lower() in ("true", "1", "yes", "on")
    cfg_data = {
        "ssid": ssid.strip(),
        "password": password.strip(),
        "band": band,
        "channel": channel,
        "failover_threshold_sec": failover_threshold_sec,
        "auto_failover_enabled": is_auto,
    }
    res = network_controller.provision_hotspot(cfg_data)
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_hotspot_card.html",
        {
            "net": net_status,
            "flash_message": res.get("message") or res.get("error"),
            "flash_type": "success" if res.get("success") else "danger"
        }
    )


@router.post("/components/network-hotspot-provision", response_class=HTMLResponse)
async def network_hotspot_provision_component(request: Request):
    """1-Click HTMX action to auto-provision hotspot in NetworkManager."""
    res = network_controller.provision_hotspot()
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_hotspot_card.html",
        {
            "net": net_status,
            "flash_message": res.get("message") or res.get("error"),
            "flash_type": "success" if res.get("success") else "danger"
        }
    )


@router.post("/components/network-sync-priorities", response_class=HTMLResponse)
async def network_sync_priorities_component(request: Request):
    """1-Click HTMX action to set all saved client Wi-Fi autoconnect priorities to 100."""
    res = network_controller.sync_client_priorities()
    net_status = network_controller.get_status()
    return render_template(
        request,
        "components/network_hotspot_card.html",
        {
            "net": net_status,
            "flash_message": res.get("message") or res.get("error"),
            "flash_type": "success" if res.get("success") else "danger"
        }
    )


# ==============================================================================
# REST API Endpoints (Part 2 Specification + Web-Driven Hotspot API)
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


@router.get("/api/network/hotspot/config")
async def api_hotspot_get_config():
    """
    GET /api/network/hotspot/config
    Returns current hotspot configuration and provisioning status.
    """
    cfg = network_controller.get_hotspot_config()
    is_prov = network_controller.is_hotspot_provisioned()
    return JSONResponse({"config": cfg, "provisioned": is_prov})


@router.post("/api/network/hotspot/config")
async def api_hotspot_save_config(payload: HotspotConfigPayload):
    """
    POST /api/network/hotspot/config
    Updates hotspot configuration and re-provisions NetworkManager profile.
    """
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    res = network_controller.provision_hotspot(update_data)
    status_code = 200 if res.get("success") else 500
    return JSONResponse(res, status_code=status_code)


@router.post("/api/network/hotspot/provision")
async def api_hotspot_provision():
    """
    POST /api/network/hotspot/provision
    1-Click auto-provisioning of Field-Hotspot in NetworkManager.
    """
    res = network_controller.provision_hotspot()
    status_code = 200 if res.get("success") else 500
    return JSONResponse(res, status_code=status_code)


@router.post("/api/network/hotspot/sync-priorities")
async def api_hotspot_sync_priorities():
    """
    POST /api/network/hotspot/sync-priorities
    Elevates autoconnect-priority to 100 on all saved client Wi-Fi networks.
    """
    res = network_controller.sync_client_priorities()
    status_code = 200 if res.get("success") else 500
    return JSONResponse(res, status_code=status_code)
