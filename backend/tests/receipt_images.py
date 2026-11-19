"""Real, tiny receipt images for upload tests (phase 11).

Since phase 11 POST /receipts accepts only files whose CONTENT is an
allow-listed image (PDF receipts are not accepted): the old fixtures that uploaded raw text
(``b"meal"``, ``scenario.encode()``) as "image/jpeg" are refused with a 415.

Many tests drive a scripted OCR stub by the uploaded content ("which scenario
is this receipt?").  ``scenario_png(text)`` therefore builds a valid 8-bit
greyscale PNG, one pixel row high, whose pixel bytes ARE the scenario text,
compressed with a *stored* (level 0) deflate block.  The text survives the
upload sanitiser (which keeps pixel data untouched) and still appears verbatim
in the stored bytes, so ``image.decode("utf-8", "ignore")`` contains it (the
local OCR provider's hint lookup) and ``scenario_of(image)`` returns exactly it
(stubs that look a scenario up by key).
"""

from __future__ import annotations

import io
import struct
import zlib
from typing import Any

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + ctype
        + body
        + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    )


def scenario_png(text: str) -> bytes:
    """A valid 1-row greyscale PNG whose pixel bytes are *text* (UTF-8)."""
    pixels = text.encode("utf-8") or b"\x00"
    ihdr = struct.pack(">IIBBBBB", len(pixels), 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00" + pixels, 0)  # filter byte 0, stored block
    return _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def scenario_of(image: bytes | str) -> str:
    """The scenario text carried by a ``scenario_png`` image (or the str key itself)."""
    if isinstance(image, str):
        return image
    if not image.startswith(_PNG_SIG):
        return image.decode("utf-8", "ignore")
    pos = len(_PNG_SIG)
    idat = b""
    while pos < len(image):
        (length,) = struct.unpack(">I", image[pos : pos + 4])
        ctype = image[pos + 4 : pos + 8]
        if ctype == b"IDAT":
            idat += image[pos + 8 : pos + 8 + length]
        pos += 12 + length
    return zlib.decompress(idat)[1:].decode("utf-8", "ignore")


def scenario_upload(text: str, name: str = "r.png") -> tuple[str, Any, str]:
    """A ``files={"file": ...}`` tuple uploading ``scenario_png(text)`` as a PNG."""
    return (name, io.BytesIO(scenario_png(text)), "image/png")


def png_name(name: str) -> str:
    """*name* with its extension switched to .png (fixtures named after JPEGs)."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}.png"
