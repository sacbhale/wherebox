"""Regenerate the binary test fixtures in this directory, deterministically.

Run: ~/.local/bin/uv run python tests/fixtures/make_fixtures.py
"""

import random
import struct
import time
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pillow_heif import register_heif_opener

HERE = Path(__file__).parent
EXIF_ORIENTATION = 0x0112

RECEIPT_LINES = [
    "CORNER GROCERY",
    "12 Main St",
    "2026-09-14  10:32",
    "",
    "MILK 1L              2 x 1.29",
    "BANANAS              1.87",
    "AA BATTERIES 8PK     6.99",
    "DISH SOAP            2.49",
    "PASTA 500G           1.19",
    "",
    "BAG                  0.10",
    "SUBTOTAL            13.92",
    "TAX                  0.84",
    "TOTAL               14.76",
    "CASH                20.00",
    "CHANGE               5.24",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def make_shelf(path: Path) -> None:
    """A 2400x1800 'shelf' with labelled boxes (long edge > 1500)."""
    rng = random.Random(42)
    im = Image.new("RGB", (2400, 1800), (222, 206, 180))
    draw = ImageDraw.Draw(im)
    for shelf_y in (600, 1200):
        draw.rectangle((0, shelf_y, 2400, shelf_y + 40), fill=(120, 84, 50))
    labels = ["PASTA", "RICE", "BEANS", "TEA", "SOAP", "TAPE", "GLUE", "BULBS", "AA"]
    for index, label in enumerate(labels):
        row, col = divmod(index, 3)
        x0 = 150 + col * 750 + rng.randint(0, 80)
        y1 = 590 + row * 600
        colour = (rng.randint(30, 220), rng.randint(30, 220), rng.randint(30, 220))
        draw.rectangle((x0, y1 - 420, x0 + 420, y1), fill=colour, outline=(20, 20, 20), width=6)
        draw.text((x0 + 40, y1 - 260), label, fill=(255, 255, 255), font=_font(90))
    im.save(path, format="JPEG", quality=90)


def _receipt_image() -> Image.Image:
    im = Image.new("RGB", (1000, 2400), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    font = _font(46)
    for index, line in enumerate(RECEIPT_LINES):
        draw.text((60, 120 + index * 120), line, fill=(0, 0, 0), font=font)
    return im


def make_receipt_jpg(path: Path) -> None:
    """A 1000x2400 receipt photo (long edge > 1500)."""
    _receipt_image().save(path, format="JPEG", quality=90)


def make_receipt_pdf(path: Path) -> None:
    """One 10x24 inch page (1500x3600 px at 150 dpi) holding the receipt text."""
    fixed = time.gmtime(1_789_381_920)  # fixed dates keep the file byte-identical
    _receipt_image().save(path, format="PDF", resolution=100.0, creationDate=fixed, modDate=fixed)


def make_iphone_heic(path: Path) -> None:
    """A 2000x1600 sensor-orientation HEIC tagged EXIF orientation 6 (rotate 90 CW).

    pillow-heif applies the rotation when decoding, so it reads as 1600x2000 portrait.
    """
    register_heif_opener()
    im = Image.new("RGB", (2000, 1600), (40, 90, 160))
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, 400, 400), fill=(230, 30, 30))  # marks the stored top-left
    draw.text((600, 700), "IPHONE", fill=(255, 255, 255), font=_font(120))
    exif = Image.Exif()
    exif[EXIF_ORIENTATION] = 6
    im.save(path, format="HEIF", quality=60, exif=exif.tobytes())


def write_bomb_png(path: Path, width: int, height: int) -> None:
    """A 1-bit, all-black PNG of width x height: huge in pixels, tiny on disk.

    Written by hand (streaming zlib) so no full-size image is ever held in memory.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)  # 1-bit grayscale
    row = b"\x00" * (1 + (width + 7) // 8)  # filter byte + packed pixels
    compressor = zlib.compressobj(9)
    parts = [compressor.compress(row) for _ in range(height)]
    parts.append(compressor.flush())
    idat = b"".join(parts)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def make_huge_png(path: Path) -> None:
    """12000x12000 = 144M pixels, above Pillow's hard 2x limit (100M) for 50M MAX_IMAGE_PIXELS."""
    write_bomb_png(path, 12_000, 12_000)


def main() -> None:
    make_shelf(HERE / "shelf.jpg")
    make_receipt_jpg(HERE / "receipt.jpg")
    make_receipt_pdf(HERE / "receipt.pdf")
    make_iphone_heic(HERE / "iphone.heic")
    make_huge_png(HERE / "huge.png")


if __name__ == "__main__":
    main()
