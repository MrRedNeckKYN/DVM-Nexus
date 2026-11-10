"""
DVM Nexus - Meshtastic Router
Web interface for device connection, messaging, node discovery, bridge routes, and remote administration.
"""

from fastapi import APIRouter, Request, Form, Response
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
from dvm_nexus.core.templates import render_template
from dvm_nexus.core.database import get_db_connection, log_audit
from dvm_nexus.meshtastic.client import meshtastic_manager
from dvm_nexus.meshtastic.bridge import meshtastic_bridge
from dvm_nexus.meshtastic.text_gateway import text_gateway
from dvm_nexus.meshtastic.transport import MockTransport

router = APIRouter(prefix="/meshtastic", tags=["meshtastic"])


@router.get("", response_class=HTMLResponse)
async def meshtastic_dashboard(request: Request):
    status = meshtastic_manager.get_status()
    nodes = meshtastic_manager.get_nodes()
    messages = meshtastic_manager.get_messages(limit=40)
    routes = meshtastic_bridge.get_routes()
    text_cfg = text_gateway.get_config()
    bridged_messages = text_gateway.get_recent_messages(limit=30)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT node_id, callsign_or_name, permission_level, can_restart_service, can_switch_profile, added_at FROM meshtastic_admins ORDER BY added_at DESC")
        admins = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return render_template(
        request,
        "meshtastic.html",
        {
            "title": "Meshtastic Integration",
            "active_tab": "meshtastic",
            "status": status,
            "nodes": nodes,
            "messages": messages,
            "routes": routes,
            "admins": admins,
            "text_cfg": text_cfg,
            "bridged_messages": bridged_messages
        }
    )


@router.post("/connect", response_class=HTMLResponse)
async def connect_transport(
    request: Request,
    transport_type: str = Form(...),
    serial_port: str = Form("auto"),
    tcp_host: str = Form("127.0.0.1"),
    tcp_port: int = Form(4403),
    ble_address: str = Form("")
):
    meshtastic_manager.save_config(
        transport_type=transport_type,
        serial_port=serial_port,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
        ble_address=ble_address,
        auto_connect=True
    )
    meshtastic_manager._init_transport()
    res = meshtastic_manager.connect()

    badge_class = "badge-success" if res.get("success") else "badge-danger"
    status_text = f"Connected ({transport_type.upper()})" if res.get("success") else f"Failed: {res.get('error')}"

    return HTMLResponse(
        f'<span class="badge {badge_class}" id="meshtastic-status-badge">{status_text}</span>'
        f'<script>location.reload();</script>'
    )


@router.post("/disconnect", response_class=HTMLResponse)
async def disconnect_transport(request: Request):
    meshtastic_manager.disconnect()
    return HTMLResponse(
        '<span class="badge badge-warning" id="meshtastic-status-badge">Disconnected</span>'
        '<script>location.reload();</script>'
    )


@router.post("/send", response_class=HTMLResponse)
async def send_message(
    request: Request,
    text: str = Form(...),
    recipient: str = Form("^all"),
    channel: int = Form(0)
):
    res = meshtastic_manager.send_message(text=text, recipient=recipient, channel=channel)
    messages = meshtastic_manager.get_messages(limit=40)
    return render_template(
        request,
        "components/meshtastic_messages.html",
        {"messages": messages}
    )


@router.get("/components/messages", response_class=HTMLResponse)
async def get_messages_partial(request: Request):
    messages = meshtastic_manager.get_messages(limit=40)
    return render_template(
        request,
        "components/meshtastic_messages.html",
        {"messages": messages}
    )


@router.get("/components/nodes", response_class=HTMLResponse)
async def get_nodes_partial(request: Request):
    nodes = meshtastic_manager.get_nodes()
    return render_template(
        request,
        "components/meshtastic_nodes.html",
        {"nodes": nodes}
    )


@router.post("/simulate-inbound", response_class=HTMLResponse)
async def simulate_inbound(
    request: Request,
    text: str = Form(...),
    sender: str = Form("!4a12bc88"),
    channel: int = Form(0)
):
    """Simulate an incoming message from a mesh node for offline testing."""
    if isinstance(meshtastic_manager.transport, MockTransport):
        meshtastic_manager.transport.simulate_inbound_message(text=text, sender=sender, channel=channel)
    else:
        # Direct trigger
        meshtastic_manager._handle_inbound_packet({
            "sender": sender,
            "recipient": "^all",
            "channel": channel,
            "text": text,
            "snr": 8.0,
            "rssi": -75,
            "timestamp": None
        })

    messages = meshtastic_manager.get_messages(limit=40)
    return render_template(
        request,
        "components/meshtastic_messages.html",
        {"messages": messages}
    )


@router.post("/routes/add", response_class=HTMLResponse)
async def add_route(
    request: Request,
    name: str = Form(...),
    source_type: str = Form(...),
    source_id: str = Form(...),
    dest_channel: int = Form(0),
    dest_node: str = Form("^all"),
    template: str = Form("{event_text}")
):
    meshtastic_bridge.add_route(
        name=name,
        source_type=source_type,
        source_id=source_id,
        dest_channel=dest_channel,
        dest_node=dest_node,
        template=template
    )
    routes = meshtastic_bridge.get_routes()
    return render_template(
        request,
        "components/meshtastic_routes.html",
        {"routes": routes}
    )


@router.post("/routes/{route_id}/delete", response_class=HTMLResponse)
async def delete_route(request: Request, route_id: int):
    meshtastic_bridge.delete_route(route_id)
    routes = meshtastic_bridge.get_routes()
    return render_template(
        request,
        "components/meshtastic_routes.html",
        {"routes": routes}
    )


@router.post("/admins/add", response_class=HTMLResponse)
async def add_admin(
    request: Request,
    node_id: str = Form(...),
    callsign_or_name: str = Form(...),
    permission_level: str = Form("read"),
    can_restart_service: Optional[str] = Form(None),
    can_switch_profile: Optional[str] = Form(None)
):
    node_id = node_id.strip()
    if not node_id.startswith("!"):
        node_id = "!" + node_id

    restart_val = 1 if can_restart_service else 0
    switch_val = 1 if can_switch_profile else 0

    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).isoformat()

    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO meshtastic_admins (node_id, callsign_or_name, permission_level, can_restart_service, can_switch_profile, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (node_id, callsign_or_name, permission_level, restart_val, switch_val, now_str))
        log_audit("MESHTASTIC_ADMIN", "ADD_ADMIN", f"Authorized admin {callsign_or_name} ({node_id}) level={permission_level}")
    finally:
        conn.close()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT node_id, callsign_or_name, permission_level, can_restart_service, can_switch_profile, added_at FROM meshtastic_admins ORDER BY added_at DESC")
        admins = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return render_template(
        request,
        "components/meshtastic_admins.html",
        {"admins": admins}
    )


@router.post("/admins/{node_id}/delete", response_class=HTMLResponse)
async def delete_admin(request: Request, node_id: str):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM meshtastic_admins WHERE node_id = ?", (node_id,))
        log_audit("MESHTASTIC_ADMIN", "REMOVE_ADMIN", f"Removed admin authorization for {node_id}")
    finally:
        conn.close()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT node_id, callsign_or_name, permission_level, can_restart_service, can_switch_profile, added_at FROM meshtastic_admins ORDER BY added_at DESC")
        admins = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return render_template(
        request,
        "components/meshtastic_admins.html",
        {"admins": admins}
    )


@router.post("/text-bridge/config", response_class=HTMLResponse)
async def update_text_bridge_config(
    request: Request,
    enabled: Optional[str] = Form(None),
    dmr_channel: int = Form(1),
    p25_channel: int = Form(2),
    default_dmr_tg: int = Form(3100),
    default_p25_tg: int = Form(101),
    forward_all_to_active: Optional[str] = Form(None),
    prefix_routing: Optional[str] = Form(None)
):
    text_gateway.save_config(
        enabled="1" if enabled else "0",
        dmr_channel=dmr_channel,
        p25_channel=p25_channel,
        default_dmr_tg=default_dmr_tg,
        default_p25_tg=default_p25_tg,
        forward_all_to_active="1" if forward_all_to_active else "0",
        prefix_routing="1" if prefix_routing else "0"
    )
    log_audit("TEXT_BRIDGE", "CONFIG_UPDATED", "Updated bidirectional text gateway settings")
    return HTMLResponse(
        '<span class="badge badge-success" id="text-bridge-status-pill">Settings Saved</span>'
        '<script>setTimeout(() => { const p = document.getElementById("text-bridge-status-pill"); if(p) p.innerText="Active"; }, 3000);</script>'
    )


@router.get("/components/bridged-messages", response_class=HTMLResponse)
async def get_bridged_messages_partial(request: Request):
    bridged_messages = text_gateway.get_recent_messages(limit=30)
    return render_template(
        request,
        "components/meshtastic_bridged_messages.html",
        {"bridged_messages": bridged_messages}
    )


@router.post("/simulate-radio-text", response_class=HTMLResponse)
async def simulate_radio_text(
    request: Request,
    protocol: str = Form("dmr"),
    src_id: int = Form(12345),
    dest_id: int = Form(3100),
    is_group: Optional[str] = Form("1"),
    text: str = Form(...)
):
    """Simulate a text message arriving over the air from a DMR or P25 radio to test Meshtastic forwarding."""
    text_gateway.handle_radio_inbound_text(
        protocol=protocol,
        src_id=src_id,
        dest_id=dest_id,
        is_group=True if is_group == "1" else False,
        text=text
    )
    bridged_messages = text_gateway.get_recent_messages(limit=30)
    return render_template(
        request,
        "components/meshtastic_bridged_messages.html",
        {"bridged_messages": bridged_messages}
    )


@router.post("/send-radio-text", response_class=HTMLResponse)
async def send_radio_text(
    request: Request,
    protocol: str = Form("dmr"),
    dest_id: int = Form(3100),
    is_group: Optional[str] = Form("1"),
    text: str = Form(...),
    forward_to_mesh: Optional[str] = Form("1")
):
    """Send text message out to radio network from WebUI and mirror to Meshtastic."""
    text_gateway.send_text_from_web(
        protocol=protocol,
        dest_id=dest_id,
        is_group=True if is_group == "1" else False,
        text=text,
        forward_to_mesh=True if forward_to_mesh == "1" else False
    )
    bridged_messages = text_gateway.get_recent_messages(limit=30)
    return render_template(
        request,
        "components/meshtastic_bridged_messages.html",
        {"bridged_messages": bridged_messages}
    )


@router.get("/api/status", response_class=JSONResponse)
async def api_status():
    return meshtastic_manager.get_status()
