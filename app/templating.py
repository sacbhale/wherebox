"""Shared Jinja2 environment. Routers render with ``templates.TemplateResponse(request, ...)``."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
