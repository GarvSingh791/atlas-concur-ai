"""Build the synthetic sample receipt used for the OCR screenshot.

The local OCR adapter (services/ocr_provider/local.py) returns canned fields for
a "meal" receipt (The Capital Grille, Chicago, 2024-11-17, USD 42.50). It picks
the profile from the bytes "ocr-profile=meal", which this script writes into the
last pixel row inside a stored (uncompressed) deflate block, the same trick as
frontend/e2e/fixtures/receipt-meal-42_50.png. The printed text matches what the
stub returns, so the image and the extracted fields agree on screen.

Usage: python make_receipt.py OUT.png
"""

from __future__ import annotations

import random
import struct
import sys
import zlib

from PIL import Image, ImageDraw, ImageFont

W, H = 640, 505
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
HINT = b"ocr-profile=meal"


def draw() -> Image.Image:
    random.seed(7)
    img = Image.new("RGB", (W, H), (252, 251, 247))
    px = img.load()
    for _ in range(900):  # paper speckle
        x, y = random.randrange(W), random.randrange(H - 1)
        px[x, y] = (236, 234, 228)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(MONO, 16)
    title, total = ImageFont.truetype(MONO_B, 26), ImageFont.truetype(MONO_B, 20)
    ink = (28, 28, 28)

    def centre(y: int, text: str, font: ImageFont.FreeTypeFont) -> None:
        w = d.textlength(text, font=font)
        d.text(((W - w) / 2, y), text, font=font, fill=ink)

    def row(y: int, left: str, right: str, font: ImageFont.FreeTypeFont = f) -> None:
        d.text((32, y), left, font=font, fill=ink)
        w = d.textlength(right, font=font)
        d.text((608 - w, y), right, font=font, fill=ink)

    centre(34, "THE CAPITAL GRILLE", title)
    centre(70, "401 N Michigan Ave, Chicago, IL", f)
    centre(96, "Check #4417   Table 8", f)
    d.text((32, 128), "-" * 59, font=f, fill=ink)
    d.text((32, 146), "Date: 2024-11-17   12:41", font=f, fill=ink)
    d.text((32, 172), "-" * 59, font=f, fill=ink)
    items = [
        ("1 Grilled salmon bowl", "24.00"),
        ("1 Sparkling water", "4.50"),
        ("1 Espresso", "3.50"),
        ("1 Side salad", "4.00"),
    ]
    for i, (name, amt) in enumerate(items):
        row(198 + 26 * i, name, amt)
    d.text((32, 302), "-" * 59, font=f, fill=ink)
    row(326, "Subtotal", "36.00")
    row(352, "Tax 10.25%", "3.69")
    row(378, "Tip", "2.81")
    row(408, "TOTAL USD", "42.50", total)
    centre(452, "VISA ****4242  APPROVED", f)
    return img


def write_png(img: Image.Image, path: str) -> None:
    rows = [bytes(img.crop((0, y, W, y + 1)).tobytes()) for y in range(H)]
    last = bytearray(rows[-1])
    last[-18:-2] = HINT  # last row, bottom-right: 16 bytes of ASCII
    rows[-1] = bytes(last)
    raw = b"".join(b"\x00" + r for r in rows)
    body, tail = raw[: -(1 + W * 3)], raw[-(1 + W * 3) :]
    co = zlib.compressobj(9)
    stream = co.compress(body) + co.flush(zlib.Z_SYNC_FLUSH)
    stream += b"\x01" + struct.pack("<HH", len(tail), len(tail) ^ 0xFFFF) + tail
    stream += struct.pack(">I", zlib.adler32(raw))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", stream)
            + chunk(b"IEND", b"")
        )


if __name__ == "__main__":
    write_png(draw(), sys.argv[1])
