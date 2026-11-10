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
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.profile_manager import profile_manager

router = APIRouter(prefix="/api")


@router.get("/status")
async def api_status():
    """Return JSON system status."""
    return JSONResponse({
        "system": get_system_metrics(),
        "service": service_controller.get_status(),
        "dvm": dvm_config_manager.get_quick_status(),
        "active_profile": profile_manager.get_active_profile(),
    })


@router.get("/system/metrics")
async def api_system_metrics():
    """Return JSON system metrics for local monitoring or remote agent telemetry."""
    return JSONResponse(get_system_metrics())


async def event_generator() -> AsyncIterator[str]:
    """SSE generator streaming system health and service status."""
    while True:
        try:
            payload = {
                "sys": get_system_metrics(),
                "service": service_controller.get_status(),
            }
            yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            pass
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
