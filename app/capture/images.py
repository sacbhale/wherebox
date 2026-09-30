"""Image preparation for AI calls (worker "Prepare" step) and HEIC display derivatives.

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

MAX_PDF_PAGES = 20
MAX_PDF_BYTES = 20 * 1024 * 1024
PDF_RENDER_PAGES = 3
PDF_DPI = 150
JPEG_QUALITY = 85


class ImageRejected(Exception):
    """The input cannot be used: not an image/PDF, corrupt, too large, or too many pages."""


def _is_pdf(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(5) == b"%PDF-"


def _check_pixels(width: int, height: int) -> None:
    max_pixels = Image.MAX_IMAGE_PIXELS or 0
    if max_pixels and width * height > max_pixels:
        raise ImageRejected(
            f"image too large: {width}x{height} pixels exceeds the {max_pixels} pixel limit"
        )


def _open_upright(path: Path) -> Image.Image:
    """Open, bomb-check, decode and EXIF-transpose an image file."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as im:
                _check_pixels(*im.size)
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
    im = _to_rgb(im)
    if max_edge is not None and max(im.size) > max_edge:
        im = im.copy()
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

    Handles JPEG/PNG/WebP/HEIC/... (EXIF-transposed) and PDF (first 3 pages at 150 dpi).
    Each output has its long edge <= ``max_edge`` (pass ``settings.ai_max_image_edge``).
    """
    path = Path(path)
    try:
        is_pdf = _is_pdf(path)
    except OSError as exc:
        raise ImageRejected(f"cannot read upload: {exc}") from exc
    if is_pdf:
        return [_encode_jpeg(page, max_edge, quality) for page in _render_pdf(path)]
    return [_encode_jpeg(_open_upright(path), max_edge, quality)]


def make_display_jpeg(
    src_path: str | Path, out_path: str | Path, *, quality: int = JPEG_QUALITY
) -> tuple[int, int]:
    """Write a full-size upright JPEG of ``src_path`` (used for HEIC). Returns (width, height)."""
    image = _open_upright(Path(src_path))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_encode_jpeg(image, None, quality))
    return image.size
