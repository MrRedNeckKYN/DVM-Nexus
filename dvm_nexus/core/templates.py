"""
DVM Nexus - Template Rendering Helper
Provides unified, version-safe Jinja2 template rendering with device detection.
"""

import re
from pathlib import Path
from typing import Dict, Any, Optional
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mobile User-Agent pattern for server-side device detection
MOBILE_UA_REGEX = re.compile(
    r"Mobile|Android|iP(hone|od|ad)|BlackBerry|IEMobile|Kindle|Silk-Accelerated|Opera M(obi|ini)",
    re.IGNORECASE
)


def is_mobile_request(request: Request) -> bool:
    """Detect if incoming HTTP request originates from a mobile device."""
    ua = request.headers.get("user-agent", "")
    return bool(MOBILE_UA_REGEX.search(ua))


def get_global_template_context() -> Dict[str, Any]:
    """Retrieve global template variables including protocol status and version."""
    try:
        from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
        proto = dvm_config_manager.get_active_protocol()
    except Exception:
        proto = "dmr"

    try:
        from dvm_nexus.config_engine.profile_manager import profile_manager
        prof = profile_manager.get_active_profile()
    except Exception:
        prof = None

    try:
        from dvm_nexus.core.config import settings
        ver = settings.app_version
    except Exception:
        ver = "1.0.8"

    try:
        from dvm_nexus.core.service_controller import service_controller
        service_info = service_controller.get_status()
    except Exception:
        service_info = None

    return {
        "active_protocol": proto,
        "active_profile": prof,
        "app_version": ver,
        "service": service_info,
    }


def render_template(
    request: Request,
    name: str,
    context: Optional[Dict[str, Any]] = None,
    status_code: int = 200
) -> Response:
    """Render a Jinja2 template with explicit device detection context and cache busting."""
    ctx = get_global_template_context()
    if context:
        ctx.update(context)
    ctx.setdefault("is_mobile", is_mobile_request(request))
    return jinja_templates.TemplateResponse(
        request=request,
        name=name,
        context=ctx,
        status_code=status_code
    )
