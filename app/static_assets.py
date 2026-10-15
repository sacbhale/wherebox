"""Cache-busted static files.

Templates link assets through ``static_url("app.css")`` -> ``/static/app.css?v=<content hash>``, so
each deploy that changes a file also changes its URL. Versioned responses may be cached for a year;
anything requested without ``v`` must be revalidated. Without this, phone browsers kept an old
app.css after a deploy (new markup, old styles).
"""

import hashlib
import os
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


# Hashed once per process (assets are fixed inside a Docker image); a dev server needs a restart
# after editing an asset for its URL to change.
@cache
def _version(path: str) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]


def static_url(path: str) -> str:
    """The versioned URL of a file under app/static (hashed once per process)."""
    return f"/static/{path}?v={_version(path)}"


class VersionedStaticFiles(StaticFiles):
    """StaticFiles that sets Cache-Control by whether the request carries a version."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        response.headers["Cache-Control"] = IMMUTABLE if versioned else REVALIDATE
        return response
