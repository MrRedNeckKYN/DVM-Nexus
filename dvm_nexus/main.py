"""
DVM Nexus - Modern Web Management Interface for DVMHost
FastAPI Application Entry Point
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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
    yield
    # Shutdown
    log_audit("SYSTEM", "SHUTDOWN", "DVM Nexus shutdown")


def create_app() -> FastAPI:
    """FastAPI Application Factory."""
    app = FastAPI(
        title="DVM Nexus",
        description="Modern Web Management Interface for DVMHost",
        version=settings.app_version,
        lifespan=lifespan,
    )

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
    app.include_router(api.router)

    return app


app = create_app()
