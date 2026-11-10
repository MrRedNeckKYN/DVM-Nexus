"""
DVM Nexus - Profile Management Router
Allows switching between saved DMR and P25 profiles with hardware mutual exclusion and rollback.
"""

import asyncio
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.profile_manager import profile_manager
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.core.service_controller import service_controller

router = APIRouter()


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request):
    """Render the Radio Profiles interface."""
    profiles = profile_manager.list_profiles()
    active_prof = profile_manager.get_active_profile()
    active_proto = dvm_config_manager.get_active_protocol()
    service_status = service_controller.get_status()

    return render_template(
        request,
        "profiles.html",
        {
            "title": "Radio Profiles",
            "active_tab": "profiles",
            "profiles": profiles,
            "active_profile": active_prof,
            "active_protocol": active_proto,
            "service": service_status,
        }
    )


@router.post("/profiles/activate/{profile_id}", response_class=HTMLResponse)
async def profiles_activate(request: Request, profile_id: str):
    """Activate a saved profile."""
    result = await asyncio.to_thread(profile_manager.activate_profile, profile_id)
    profiles = profile_manager.list_profiles()
    active_prof = profile_manager.get_active_profile()
    service_status = service_controller.get_status()

    toast_type = "success" if result.get("success") else "error"
    toast_msg = result.get("message") or result.get("error")

    return render_template(
        request,
        "components/profile_list.html",
        {
            "profiles": profiles,
            "active_profile": active_prof,
            "service": service_status,
            "toast": {"type": toast_type, "message": toast_msg}
        }
    )


@router.post("/profiles/create", response_class=HTMLResponse)
async def profiles_create(
    request: Request,
    name: str = Form(...),
    protocol: str = Form(...),
    description: str = Form(""),
):
    """Save current configuration as a new named profile."""
    profile_id = profile_manager.save_current_as_profile(
        name=name.strip(),
        protocol=protocol.lower().strip(),
        description=description.strip()
    )
    profiles = profile_manager.list_profiles()
    active_prof = profile_manager.get_active_profile()
    service_status = service_controller.get_status()

    return render_template(
        request,
        "components/profile_list.html",
        {
            "profiles": profiles,
            "active_profile": active_prof,
            "service": service_status,
            "toast": {"type": "success", "message": f"Created profile '{name}'"}
        }
    )


@router.post("/profiles/delete/{profile_id}", response_class=HTMLResponse)
async def profiles_delete(request: Request, profile_id: str):
    """Delete a non-active profile."""
    try:
        profile_manager.delete_profile(profile_id)
        profiles = profile_manager.list_profiles()
        active_prof = profile_manager.get_active_profile()
        service_status = service_controller.get_status()
        return render_template(
            request,
            "components/profile_list.html",
            {
                "profiles": profiles,
                "active_profile": active_prof,
                "service": service_status,
                "toast": {"type": "success", "message": "Profile deleted successfully"}
            }
        )
    except Exception as e:
        profiles = profile_manager.list_profiles()
        active_prof = profile_manager.get_active_profile()
        service_status = service_controller.get_status()
        return render_template(
            request,
            "components/profile_list.html",
            {
                "profiles": profiles,
                "active_profile": active_prof,
                "service": service_status,
                "toast": {"type": "error", "message": str(e)}
            }
        )


@router.post("/api/protocol/switch")
async def protocol_switch_endpoint(
    request: Request,
    target: str = Form(None),
):
    """
    Switch active radio protocol between DMR and P25.
    Accepts target protocol via Form data or JSON body.
    """
    from fastapi.responses import JSONResponse
    target_proto = target
    if not target_proto:
        try:
            body = await request.json()
            target_proto = body.get("target") or body.get("protocol")
        except Exception:
            pass

    if not target_proto:
        return JSONResponse(status_code=400, content={"success": False, "error": "Target protocol ('dmr' or 'p25') required"})

    result = await asyncio.to_thread(profile_manager.switch_protocol_mode, target_proto)
    status_code = 200 if result.get("success") else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "success": result.get("success", False),
            "message": result.get("message") or result.get("error"),
            "target": target_proto.lower(),
            "redirect": f"/{target_proto.lower()}"
        }
    )
