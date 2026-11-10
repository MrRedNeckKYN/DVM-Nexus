"""
DVM Nexus - Profile Management Router
Allows switching between saved DMR and P25 profiles with hardware mutual exclusion and rollback.
"""

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
    result = profile_manager.activate_profile(profile_id)
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
