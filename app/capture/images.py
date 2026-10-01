"""Image preparation for AI calls (worker "Prepare" step) and display derivatives.

Geometry invariant (spec-multiphoto R16): the display JPEG is the EXIF-transposed original
scaled with its aspect ratio kept (long edge DISPLAY_MAX_EDGE), and the worker prepares the
AI image from that display JPEG (again aspect-preserving). So a box the model gives in
0..1000 units of an image maps 1:1 onto the display JPEG, whatever the original's EXIF
orientation and whatever the browser does with EXIF.

Every rejection raises ``ImageRejected`` with a message fit for ``captures.error``.
"""

import io
import warnings
from pathlib import Path

import pypdfium2
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()
# Decompression-bomb guard. Anything above this pixel count is rejected outright (Pillow
# itself only warns between 1x and 2x this value, so prepare checks the size explicitly).
Image.MAX_IMAGE_PIXELS = 50_000_000

# The only image formats ever decoded (what phones and browsers produce; Pillow's JPEG
# opener also takes multi-picture phone JPEGs, MPO). Anything else, even renamed .jpg, is
# refused without being decoded.
PHOTO_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "HEIF")

MAX_PDF_PAGES = 20
MAX_PDF_BYTES = 20 * 1024 * 1024
PDF_RENDER_PAGES = 3
PDF_DPI = 150
JPEG_QUALITY = 85
DISPLAY_MAX_EDGE = 2000
# The provider call timeout grows by this much per image after the first.
TIMEOUT_PER_EXTRA_IMAGE_S = 10


class ImageRejected(Exception):
    """The input cannot be used: not an image/PDF, corrupt, too large, or too many pages."""


def _is_pdf(path: Path) -> bool:
    """True when the file starts like a PDF; ImageRejected when it cannot be read."""
    try:
        with path.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError as exc:
        raise ImageRejected(f"cannot read upload: {exc}") from exc


def _check_pixels(width: int, height: int) -> None:
    max_pixels = Image.MAX_IMAGE_PIXELS or 0
    if max_pixels and width * height > max_pixels:
        raise ImageRejected(
            f"image too large: {width}x{height} pixels exceeds the {max_pixels} pixel limit"
        )


def _fit(size: tuple[int, int], max_edge: int) -> tuple[int, int]:
    """``size`` scaled so its long edge is ``max_edge`` (never enlarged)."""
    width, height = size
    scale = min(max_edge / max(width, height, 1), 1.0)
    return max(round(width * scale), 1), max(round(height * scale), 1)


def _open_upright(path: Path, max_edge: int | None = None) -> Image.Image:
    """Open (PHOTO_FORMATS only), bomb-check, decode and EXIF-transpose an image file.

    With ``max_edge`` the result's long edge is at most that (aspect ratio kept): a JPEG is
    decoded at a reduced scale (``draft``) and the image is shrunk before it is transposed,
    which gives the same picture as transposing first, with far less memory."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=PHOTO_FORMATS) as im:
                _check_pixels(*im.size)
                if max_edge is not None:
                    im.draft("RGB", _fit(im.size, max_edge))
                    im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                else:
                    im.load()
                upright = ImageOps.exif_transpose(im)
    except ImageRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageRejected(f"image too large: {exc}") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageRejected(f"not a readable image: {exc}") from exc
    return upright


def _to_rgb(im: Image.Image) -> Image.Image:
    if im.mode == "RGB":
        return im
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return im.convert("RGB")


def _encode_jpeg(im: Image.Image, max_edge: int | None, quality: int = JPEG_QUALITY) -> bytes:
    """``im`` is the caller's own image (it may be shrunk in place)."""
    im = _to_rgb(im)
    if max_edge is not None and max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    im.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _render_pdf(path: Path) -> list[Image.Image]:
    size = path.stat().st_size
    if size > MAX_PDF_BYTES:
        raise ImageRejected(f"PDF too large: {size} bytes (limit {MAX_PDF_BYTES})")
    try:
        pdf = pypdfium2.PdfDocument(path)
    except pypdfium2.PdfiumError as exc:
        raise ImageRejected(f"not a readable PDF: {exc}") from exc
    try:
        pages = len(pdf)
        if pages == 0:
            raise ImageRejected("PDF has no pages")
        if pages > MAX_PDF_PAGES:
            raise ImageRejected(f"PDF has {pages} pages (limit {MAX_PDF_PAGES})")
        scale = PDF_DPI / 72
        images: list[Image.Image] = []
        for index in range(min(pages, PDF_RENDER_PAGES)):
            page = pdf[index]
            try:
                width, height = page.get_size()
                _check_pixels(round(width * scale), round(height * scale))
                bitmap = page.render(scale=scale)
                images.append(bitmap.to_pil().convert("RGB"))
            finally:
                page.close()
        return images
    except pypdfium2.PdfiumError as exc:
        raise ImageRejected(f"PDF could not be rendered: {exc}") from exc
    finally:
        pdf.close()


def prepare_images(
    path: str | Path, *, max_edge: int = 1500, quality: int = JPEG_QUALITY
) -> list[bytes]:
    """JPEG bytes ready for ``ExtractRequest.images``: one per image, up to 3 per PDF.

    Handles PHOTO_FORMATS (EXIF-transposed) and PDF (first 3 pages at 150 dpi).
    Each output has its long edge <= ``max_edge`` (pass ``settings.ai_max_image_edge``).
    """
    path = Path(path)
    if _is_pdf(path):
        return [_encode_jpeg(page, max_edge, quality) for page in _render_pdf(path)]
    return [_encode_jpeg(_open_upright(path, max_edge), None, quality)]


def make_display_jpeg(
    src_path: str | Path,
    out_path: str | Path,
    *,
    max_edge: int = DISPLAY_MAX_EDGE,
    quality: int = JPEG_QUALITY,
) -> tuple[int, int]:
    """Write the upright display JPEG of ``src_path`` (long edge <= ``max_edge``, aspect ratio
    kept). Returns its (width, height)."""
    image = _to_rgb(_open_upright(Path(src_path), max_edge))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_encode_jpeg(image, None, quality))
    return image.size


def call_timeout(base_s: float, images: int) -> float:
    """The provider call timeout for a request with ``images`` images:
    AI_TIMEOUT_SECONDS + 10 s for every image after the first."""
    return base_s + TIMEOUT_PER_EXTRA_IMAGE_S * max(images - 1, 0)
