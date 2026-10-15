"""Shared Jinja2 environment. Routers render with ``templates.TemplateResponse(request, ...)``."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.capture import gallery, groups
from app.config import APP_NAME
from app.static_assets import static_url

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def usd(value: float | None) -> str:
    """A USD amount for display: "$1.25", "$0.50", smaller amounts with 3 significant digits
    ("$0.000375", "<$0.0001" below that), "$0" for zero and "?" when unknown."""
    if value is None:
        return "?"
    if value == 0:
        return "$0"
    if value >= 0.1:
        return f"${value:,.2f}"
    return f"${value:.3g}" if value >= 0.0001 else "<$0.0001"


templates.env.filters["usd"] = usd
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["static_url"] = static_url

# The annotated gallery's view model (app/capture/gallery.py), for partials/gallery.html.
templates.env.globals["group_frames"] = gallery.group_frames
templates.env.globals["sighting_frames"] = gallery.sighting_frames
templates.env.globals["report_frames"] = gallery.report_frames
templates.env.globals["max_photos_per_send"] = groups.MAX_PHOTOS_PER_SUBMISSION
templates.env.globals["max_group_photos"] = groups.MAX_GROUP_PHOTOS
