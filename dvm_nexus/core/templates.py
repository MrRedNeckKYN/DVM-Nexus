"""
DVM Nexus - Template Rendering Helper
Provides unified, version-safe Jinja2 template rendering.
"""

from pathlib import Path
from typing import Dict, Any, Optional
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render_template(
    request: Request,
    name: str,
    context: Optional[Dict[str, Any]] = None,
    status_code: int = 200
) -> Response:
    """Render a Jinja2 template with explicit keyword arguments for Starlette compatibility."""
    ctx = context or {}
    return jinja_templates.TemplateResponse(
        request=request,
        name=name,
        context=ctx,
        status_code=status_code
    )
