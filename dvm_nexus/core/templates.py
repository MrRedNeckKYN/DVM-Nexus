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


def render_template(
    request: Request,
    name: str,
    context: Optional[Dict[str, Any]] = None,
    status_code: int = 200
) -> Response:
    """Render a Jinja2 template with explicit device detection context and cache busting."""
    ctx = context or {}
    ctx.setdefault("is_mobile", is_mobile_request(request))
    ctx.setdefault("app_version", "1.0.1")
    return jinja_templates.TemplateResponse(
        request=request,
        name=name,
        context=ctx,
        status_code=status_code
    )
