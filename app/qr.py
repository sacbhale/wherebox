"""QR codes for permanent location URLs (segno, error correction level M)."""

import io

import segno

PNG_SCALE_MIN = 1
PNG_SCALE_MAX = 40


def _make(url: str) -> segno.QRCode:
    # make_qr: always a full QR code (never Micro QR); boost_error=False keeps level M exactly.
    return segno.make_qr(url, error="m", boost_error=False)


def svg(url: str, scale: int = 8) -> bytes:
    out = io.BytesIO()
    _make(url).save(out, kind="svg", scale=scale, border=4, xmldecl=True)
    return out.getvalue()


def svg_inline(url: str, scale: int = 6, omitsize: bool = False) -> str:
    """An ``<svg>`` element for embedding in HTML (the print labels). ``omitsize`` gives it a
    viewBox instead of a fixed width and height, so CSS can size it."""
    return _make(url).svg_inline(scale=scale, border=4, omitsize=omitsize)


def png(url: str, scale: int = 10) -> bytes:
    if not PNG_SCALE_MIN <= scale <= PNG_SCALE_MAX:
        raise ValueError(f"scale must be {PNG_SCALE_MIN}..{PNG_SCALE_MAX}")
    out = io.BytesIO()
    _make(url).save(out, kind="png", scale=scale, border=4)
    return out.getvalue()
