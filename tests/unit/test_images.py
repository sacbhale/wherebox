"""Image preparation: downscale, EXIF transpose, HEIC, PDF page caps, bomb rejection (AC-22)."""

import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image, JpegImagePlugin

from app.capture import images
from app.capture.images import ImageRejected, make_display_jpeg, prepare_images

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_fixture_maker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_fixtures = _load_fixture_maker()


def _decode(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG"
    return im


def _source_long_edge(path: Path) -> int:
    with Image.open(path) as im:
        return max(im.size)


@pytest.mark.parametrize("name", ["shelf.jpg", "receipt.jpg", "iphone.heic"])
def test_image_inputs_are_downscaled_jpeg(name: str) -> None:
    path = FIXTURES / name
    assert _source_long_edge(path) > 1500  # the fixture really needs downscaling
    out = prepare_images(path, max_edge=1500)
    assert len(out) == 1
    im = _decode(out[0])
    assert max(im.size) == 1500
    assert im.mode == "RGB"


def test_pdf_receipt_is_rendered_and_downscaled() -> None:
    out = prepare_images(FIXTURES / "receipt.pdf", max_edge=1500)
    assert len(out) == 1
    im = _decode(out[0])
    assert max(im.size) == 1500  # 150 dpi render of a 24in page is 3600px before downscale


def test_max_edge_setting_is_honoured() -> None:
    im = _decode(prepare_images(FIXTURES / "shelf.jpg", max_edge=800)[0])
    assert im.size == (800, 600)


def test_small_images_are_not_upscaled(tmp_path: Path) -> None:
    path = tmp_path / "small.png"
    Image.new("RGBA", (300, 200), (0, 0, 0, 0)).save(path)
    im = _decode(prepare_images(path)[0])
    assert im.size == (300, 200)
    assert im.getpixel((10, 10)) == (255, 255, 255)  # transparency flattened onto white


def test_heic_is_upright() -> None:
    # Stored 2000x1600 with orientation 6: it must come out portrait.
    im = _decode(prepare_images(FIXTURES / "iphone.heic")[0])
    assert im.size == (1200, 1500)


def test_jpeg_exif_orientation_is_applied(tmp_path: Path) -> None:
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[make_fixtures.EXIF_ORIENTATION] = 6
    Image.new("RGB", (2000, 1000), (10, 200, 10)).save(path, exif=exif.tobytes())
    im = _decode(prepare_images(path)[0])
    assert im.size == (750, 1500)


def test_heic_display_jpeg(tmp_path: Path) -> None:
    out = tmp_path / "display" / "iphone.jpg"
    size = make_display_jpeg(FIXTURES / "iphone.heic", out)
    assert size == (1600, 2000)  # full size, upright
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (1600, 2000)


def test_decompression_bomb_fixture_is_rejected() -> None:
    assert (FIXTURES / "huge.png").stat().st_size < 100_000
    with pytest.raises(ImageRejected, match="too large"):
        prepare_images(FIXTURES / "huge.png")


def test_bomb_between_limit_and_double_limit_is_rejected(tmp_path: Path) -> None:
    # 60M pixels: Pillow itself only warns here; prepare must still reject.
    path = tmp_path / "sixty.png"
    make_fixtures.write_bomb_png(path, 10_000, 6_000)
    with pytest.raises(ImageRejected, match="too large"):
        prepare_images(path)
    with pytest.raises(ImageRejected):
        make_display_jpeg(path, tmp_path / "out.jpg")


def test_max_image_pixels_is_set() -> None:
    assert Image.MAX_IMAGE_PIXELS == 50_000_000


def _pdf(path: Path, pages: int) -> Path:
    frames = [Image.new("RGB", (400, 600), (255, 255, 255)) for _ in range(pages)]
    frames[0].save(path, format="PDF", save_all=True, append_images=frames[1:], resolution=72.0)
    return path


def test_pdf_renders_at_most_three_pages(tmp_path: Path) -> None:
    out = prepare_images(_pdf(tmp_path / "five.pdf", 5))
    assert len(out) == 3
    # 400x600 pt at 150 dpi is about 833x1250 px (pdfium rounds up), below the edge cap.
    assert _decode(out[0]).size[1] == 1250


def test_pdf_with_twenty_pages_is_accepted(tmp_path: Path) -> None:
    assert len(prepare_images(_pdf(tmp_path / "twenty.pdf", 20))) == 3


def test_pdf_over_twenty_pages_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageRejected, match="21 pages"):
        prepare_images(_pdf(tmp_path / "many.pdf", 21))


def test_pdf_over_twenty_mb_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _pdf(tmp_path / "big.pdf", 1)
    monkeypatch.setattr(images, "MAX_PDF_BYTES", path.stat().st_size - 1)
    with pytest.raises(ImageRejected, match="PDF too large"):
        prepare_images(path)


def test_twenty_mb_limit_value() -> None:
    assert images.MAX_PDF_BYTES == 20 * 1024 * 1024
    assert images.MAX_PDF_PAGES == 20
    assert images.PDF_RENDER_PAGES == 3
    assert images.PDF_DPI == 150


@pytest.mark.parametrize(
    "content", [b"", b"not an image at all", b"%PDF-1.7\nbroken", b"\x89PNG\r\n\x1a\ntruncated"]
)
def test_garbage_is_rejected(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "upload.bin"
    path.write_bytes(content)
    with pytest.raises(ImageRejected):
        prepare_images(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageRejected):
        prepare_images(tmp_path / "gone.jpg")


@pytest.mark.parametrize("kind", ["BMP", "TIFF", "PPM"])
def test_formats_phones_do_not_produce_are_refused_undecoded(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "renamed.jpg"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(path, format=kind)
    with pytest.raises(ImageRejected, match="not a readable image"):
        prepare_images(path)
    with pytest.raises(ImageRejected, match="not a readable image"):
        make_display_jpeg(path, tmp_path / "out.jpg")
    assert not (tmp_path / "out.jpg").exists()


def test_big_jpeg_is_decoded_at_reduced_scale_and_shrunk_before_transposing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4080x3072 phone JPEG (orientation 6) for a 2000 px display copy: decoded at half
    scale (draft), shrunk, then transposed: upright 1506x2000, left half red as stored."""
    path = tmp_path / "phone.jpg"
    stored = Image.new("RGB", (4080, 3072), (20, 20, 220))
    stored.paste((220, 20, 20), (0, 0, 2040, 3072))  # stored left half red
    exif = Image.Exif()
    exif[make_fixtures.EXIF_ORIENTATION] = 6  # displayed rotated 90 degrees clockwise
    stored.save(path, exif=exif.tobytes(), quality=90)
    drafts: list[tuple[tuple[int, int], tuple[int, int]]] = []
    original_draft = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):  # type: ignore[no-untyped-def]
        result = original_draft(self, mode, size)
        if mode == "RGB":
            drafts.append((size, self.size))
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    out = tmp_path / "display.jpg"
    assert make_display_jpeg(path, out) == (1506, 2000)
    # Asked for the stored-orientation target; decoded at half scale (2040x1536).
    assert drafts == [((2000, 1506), (2040, 1536))]
    with Image.open(out) as im:
        rgb = im.convert("RGB")
        # Rotated clockwise: the stored left half is now the top half.
        assert rgb.getpixel((753, 300))[0] > 150 and rgb.getpixel((753, 1700))[2] > 150
