"""Django-style origin checking for unsafe methods, plus the htmx header requirement.

POST/PUT/PATCH/DELETE must carry an ``Origin`` (or, when absent, ``Referer``) whose normalized
``(host, port)`` equals the effective request Host (after uvicorn's proxy handling), the
``PUBLIC_BASE_URL`` host, or an ``ALLOWED_HOSTS`` entry. Otherwise the request gets 403.
"""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.config import Settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_PORTS = {"http": 80, "https": 443}

HostPort = tuple[str, int]


def origin_pair(url: str) -> HostPort | None:
    """``scheme://host[:port]...`` -> (lowercase host, port with scheme default)."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    return parts.hostname.lower(), port or DEFAULT_PORTS[parts.scheme]


def allowed_pairs(request: Request) -> set[HostPort]:
    settings: Settings = request.app.state.settings
    pairs: set[HostPort] = set()
    host_header = request.headers.get("host")
    if host_header:
        pair = origin_pair(f"{request.url.scheme}://{host_header}")
        if pair:
            pairs.add(pair)
    pair = origin_pair(settings.public_base_url)
    if pair:
        pairs.add(pair)
    for entry in settings.allowed_hosts_list:
        if "://" in entry:
            pair = origin_pair(entry)
            if pair:
                pairs.add(pair)
            continue
        with_port = origin_pair(f"http://{entry}")
        if with_port is None:
            continue
        if urlsplit(f"http://{entry}").port is None:
            # A bare hostname is accepted on both default ports.
            pairs.update({(with_port[0], 80), (with_port[0], 443)})
        else:
            pairs.add(with_port)
    return pairs


def origin_allowed(request: Request) -> bool:
    source = request.headers.get("origin")
    if source is None:
        source = request.headers.get("referer")
    if not source:
        return False
    pair = origin_pair(source)
    return pair is not None and pair in allowed_pairs(request)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def require_htmx(request: Request) -> None:
    """Dependency for htmx mutation endpoints: demand ``HX-Request: true`` (else 403)."""
    if not is_htmx(request):
        raise HTTPException(status_code=403, detail="htmx request required")
