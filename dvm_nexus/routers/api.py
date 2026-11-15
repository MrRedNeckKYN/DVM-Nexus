"""
DVM Nexus - REST & SSE Streaming API Router
"""

import asyncio
import json
from typing import AsyncIterator
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from dvm_nexus.core.system_monitor import get_system_metrics
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.telemetry_service import telemetry_service
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.profile_manager import profile_manager

import logging

logger = logging.getLogger("dvm_nexus.api")
router = APIRouter(prefix="/api")


@router.get("/status")
async def api_status():
    """Return JSON system status, live RF HUD state, and Connected Radios roster."""
    radios = telemetry_service.get_connected_radios(limit=50)
    return JSONResponse({
        "system": get_system_metrics(),
        "service": service_controller.get_status(),
        "dvm": dvm_config_manager.get_quick_status(),
        "active_profile": profile_manager.get_active_profile(),
        "hud": telemetry_service.get_hud_status(),
        "connected_radios": radios,
        "connected_radios_count": len(radios),
    })


@router.get("/radios/roster")
async def api_radios_roster():
    """Return JSON list of currently affiliated, registered, or active subscriber radios."""
    radios = telemetry_service.get_connected_radios(limit=100)
    return JSONResponse({
        "radios": radios,
        "count": len(radios),
        "hud": telemetry_service.get_hud_status(),
    })


@router.get("/system/metrics")
async def api_system_metrics():
    """Return JSON system metrics for local monitoring or remote agent telemetry."""
    return JSONResponse(get_system_metrics())


def _collect_stream_payload() -> dict:
    return {
        "sys": get_system_metrics(),
        "service": service_controller.get_status(),
        "hud": telemetry_service.get_hud_status(),
        "connected_radios": telemetry_service.get_connected_radios(limit=50),
    }


async def event_generator() -> AsyncIterator[str]:
    """SSE generator streaming system health and service status (blocking work runs in a thread)."""
    while True:
        try:
            payload = await asyncio.to_thread(_collect_stream_payload)
            yield f"data: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Error in SSE stream generator: {exc}")
            yield f"event: error\ndata: {json.dumps({'error': 'telemetry unavailable'})}\n\n"
        await asyncio.sleep(2.0)


@router.get("/stream/events")
async def sse_events():
    """Server-Sent Events endpoint for real-time frontend monitoring."""
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
