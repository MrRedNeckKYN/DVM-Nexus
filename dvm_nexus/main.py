"""
DVM Nexus - Modern Web Management Interface for DVMHost
FastAPI Application Entry Point
"""

import os
import asyncio
import secrets
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from dvm_nexus.core.config import settings
from dvm_nexus.core.database import init_db, log_audit
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.profile_manager import profile_manager

# Import routers
from dvm_nexus.routers import (
    dashboard,
    wizard,
    dmr,
    p25,
    profiles,
    config_editor,
    modem,
    api,
    meshtastic_router,
    updater_router,
    network_router,
    diagnostics_router,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # Startup: Ensure database and active config directory are ready
    init_db()
    dvm_config_manager.ensure_active_config_exists()
    log_audit("SYSTEM", "STARTUP", f"DVM Nexus v{settings.app_version} initialized")

    # Auto-provision Field Hotspot and launch embedded failover watchdog
    watchdog_task = None
    try:
        from dvm_nexus.core.network_manager import network_controller
        network_controller.ensure_hotspot_provisioned()
        watchdog_task = asyncio.create_task(network_controller.run_watchdog_loop())
    except Exception:
        pass

    yield
    # Shutdown
    if watchdog_task:
        watchdog_task.cancel()
    log_audit("SYSTEM", "SHUTDOWN", "DVM Nexus shutdown")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Optional HTTP Basic Authentication for remote web access."""
    async def dispatch(self, request, call_next):
        if not settings.auth_enabled:
            return await call_next(request)
        if request.url.path.startswith("/static"):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            return Response(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="DVM Nexus Admin"'}
            )

        try:
            encoded_credentials = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded_credentials).decode("utf-8")
            username, _, password = decoded.partition(":")
            correct_user = secrets.compare_digest(username, settings.auth_username)
            correct_password = secrets.compare_digest(password, settings.auth_password)
            if not (correct_user and correct_password):
                return Response(
                    "Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="DVM Nexus Admin"'}
                )
        except Exception:
            return Response(
                "Invalid authorization header",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="DVM Nexus Admin"'}
            )

        return await call_next(request)


def create_app() -> FastAPI:
    """FastAPI Application Factory."""
    app = FastAPI(
        title="DVM Nexus",
        description="Modern Web Management Interface for DVMHost",
        version=settings.app_version,
        lifespan=lifespan,
    )

    # Optional Basic Auth
    app.add_middleware(BasicAuthMiddleware)

    # Static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Include routers
    app.include_router(dashboard.router)
    app.include_router(wizard.router)
    app.include_router(dmr.router)
    app.include_router(p25.router)
    app.include_router(profiles.router)
    app.include_router(config_editor.router)
    app.include_router(modem.router)
    app.include_router(meshtastic_router.router)
    app.include_router(updater_router.router)
    app.include_router(network_router.router)
    app.include_router(diagnostics_router.router)
    app.include_router(api.router)

    return app


app = create_app()
