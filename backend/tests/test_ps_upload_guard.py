"""Phase 11 (public-demo security): upload size + type checks — hermetic.

Covers the request-body middleware (Content-Length, lying / absent length,
bounded buffering, the upload route's larger limit, nosniff), the content
sniffing / mismatch rules, structural image checks (empty, truncated, corrupt,
zero-size, decompression bombs), metadata stripping (EXIF GPS removed,
orientation kept), PDF rasterisation rules (phase 12), file-name
hygiene and the presigned-link response headers.  No database: the real app
is driven without its lifespan.
"""

from __future__ import annotations

import asyncio
import io
import random
import struct
import subprocess
import sys
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request, UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.core.body_limit import (
    MULTIPART_OVERHEAD_BYTES,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.errors import http_exception_handler
from backend.app.main_entry.app import app as real_app
from backend.app.modules.receipt import upload_guard as g
from backend.tests.receipt_images import scenario_of, scenario_png
from config.settings import get_settings
from services.storage_provider.real import served_headers_for_key

# ---------------------------------------------------------------------------
# Fixtures: real, tiny files of every allowed type
# ---------------------------------------------------------------------------


def _jpeg(size: tuple[int, int] = (64, 32), *, exif: Image.Exif | None = None, **kw: Any) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "JPEG", exif=exif or Image.Exif(), **kw)
    return buf.getvalue()


def _png(size: tuple[int, int] = (40, 20), **kw: Any) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 200, 10)).save(buf, "PNG", **kw)
    return buf.getvalue()


def _webp(size: tuple[int, int] = (40, 20), *, exif: Image.Exif | None = None) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 10, 200)).save(buf, "WEBP", exif=exif or Image.Exif())
    return buf.getvalue()


def _gps_exif(orientation: int = 6) -> Image.Exif:
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x0110] = "PhoneModel X"  # Model
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 44.0, 54.36)
    gps[3] = "W"
    gps[4] = (73.0, 59.0, 8.5)
    return exif


def _inspect(data: bytes, name: str | None = None, ctype: str | None = None) -> g.CheckedUpload:
    return g.inspect_upload(data, filename=name, content_type=ctype)


def _chunk(ctype: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)


def _png_with_header(width: int, height: int, idat: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# 1. Allowed types pass, by content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "name", "ctype", "kind"),
    [
        (_jpeg(), "receipt.jpg", "image/jpeg", "jpeg"),
        (_jpeg(), "RECEIPT.JPEG", "image/jpg", "jpeg"),
        (_png(), "receipt.png", "image/png", "png"),
        (_webp(), "receipt.webp", "image/webp", "webp"),
        # The client may not say what it is: content decides.
        (_png(), "blob", "application/octet-stream", "png"),
        (_jpeg(), None, None, "jpeg"),
    ],
)
def test_each_allowed_type_passes(
    data: bytes, name: str | None, ctype: str | None, kind: str
) -> None:
    checked = _inspect(data, name, ctype)
    assert checked.kind == kind
    assert checked.content_type == g.CONTENT_TYPES[kind]


def test_progressive_jpeg_and_palette_png_pass() -> None:
    assert _inspect(_jpeg(progressive=True), "a.jpg").kind == "jpeg"
    buf = io.BytesIO()
    Image.new("P", (30, 10)).save(buf, "PNG")
    assert _inspect(buf.getvalue(), "a.png").kind == "png"


def test_png_pixels_are_untouched() -> None:
    """Sanitising is lossless: no re-encode, OCR sees the original pixels."""
    data = _png()
    assert _inspect(data, "a.png").data == data
    scenario = scenario_png("meal60")
    assert scenario_of(_inspect(scenario, "r.png").data) == "meal60"


# ---------------------------------------------------------------------------
# 2. Type mismatches and non-allow-listed content -> 415
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "name", "ctype"),
    [
        (_png(), "receipt.jpg", None),  # extension alone disagrees
        (_png(), "receipt.png", "image/jpeg"),  # claimed type disagrees
        (_jpeg(), "receipt.png", "image/png"),
        (b"just some text, not a receipt", "receipt.jpg", "image/jpeg"),  # text as .jpg
        (b"GIF89a\x01\x00\x01\x00" + b"\x00" * 20, "r.gif", "image/gif"),  # GIF not allowed
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "r.svg", "image/svg+xml"),
        (b"<html><script>alert(1)</script></html>", "r.html", "text/html"),
        (_png(), "receipt.png", "text/html"),  # unknown claimed type
        (_png(), "receipt.exe", "image/png"),  # dangerous extension
        (_png(), "receipt.png.html", "image/png"),
    ],
)
def test_type_mismatch_is_415(data: bytes, name: str, ctype: str | None) -> None:
    with pytest.raises(g.UnsupportedMediaTypeError) as exc:
        _inspect(data, name, ctype)
    assert exc.value.status_code == 415
    assert exc.value.code == "UNSUPPORTED_MEDIA_TYPE"


def test_polyglot_png_with_trailing_payload_is_stripped() -> None:
    """A valid PNG followed by an HTML/ZIP payload: the payload never reaches storage."""
    payload = b"<html><script>alert(document.cookie)</script></html>PK\x03\x04zip"
    checked = _inspect(_png() + payload, "a.png", "image/png")
    assert payload not in checked.data
    assert checked.data.endswith(_chunk(b"IEND", b""))


def test_polyglot_jpeg_with_trailing_payload_is_stripped() -> None:
    payload = b"%PDF-1.7 hidden pdf after the image"
    checked = _inspect(_noisy(_jpeg) + payload, "a.jpg")
    assert payload not in checked.data
    assert checked.data.endswith(b"\xff\xd9")


def test_png_magic_followed_by_garbage_is_rejected() -> None:
    with pytest.raises(g.InvalidUploadError):
        _inspect(b"\x89PNG\r\n\x1a\n<html>not really a png</html>", "a.png")


# ---------------------------------------------------------------------------
# 3. Empty, truncated, corrupt, zero-size -> 422
# ---------------------------------------------------------------------------


def test_zero_byte_file_is_rejected() -> None:
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(b"", "a.png", "image/png")
    assert exc.value.status_code == 422
    assert exc.value.code == "INVALID_UPLOAD"


@pytest.mark.parametrize("make", [_png, _jpeg, _webp])
def test_truncated_image_is_rejected(make: Any) -> None:
    data = make()
    with pytest.raises(g.InvalidUploadError):
        _inspect(data[: len(data) // 2])


def test_png_with_bad_crc_is_rejected() -> None:
    """A kept chunk (gAMA) whose CRC does not match: only the CRC check sees it."""
    good = scenario_png("crc")
    gama = _chunk(b"gAMA", struct.pack(">I", 45455))
    bad_gama = gama[:-4] + bytes(b ^ 0xFF for b in gama[-4:])
    at = good.index(b"IDAT") - 4
    assert _inspect(good[:at] + gama + good[at:], "a.png").kind == "png"
    with pytest.raises(g.InvalidUploadError):
        _inspect(good[:at] + bad_gama + good[at:], "a.png")


def test_inspect_rejects_data_over_the_upload_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _png()
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_BYTES", len(data) - 1)
    with pytest.raises(g.PayloadTooLargeError) as exc:
        _inspect(data, "a.png")
    assert exc.value.status_code == 413


def test_png_with_corrupt_pixel_stream_is_rejected() -> None:
    """Valid chunk CRCs but a deflate stream that does not hold the declared pixels."""
    with pytest.raises(g.InvalidUploadError):
        _inspect(_png_with_header(10, 10, zlib.compress(b"\x00" * 5)), "a.png")


def _strip_segment(data: bytes, marker: bytes) -> bytes:
    at = data.index(marker)
    length = int.from_bytes(data[at + 2 : at + 4], "big")
    return data[:at] + data[at + 2 + length :]


def test_jpeg_without_quantisation_tables_is_rejected() -> None:
    """Structurally parseable, but undecodable: caught by the (1/8-scale) decode check."""
    with pytest.raises(g.InvalidUploadError):
        _inspect(_strip_segment(_jpeg((256, 256)), b"\xff\xdb"), "a.jpg")


def test_jpeg_scan_without_image_data_is_rejected() -> None:
    data = _jpeg((256, 256))
    sos = data.index(b"\xff\xda")
    header_end = sos + 2 + int.from_bytes(data[sos + 2 : sos + 4], "big")
    with pytest.raises(g.InvalidUploadError):
        _inspect(data[:header_end] + b"\xff\xd9", "a.jpg")


def test_zero_dimension_image_is_rejected() -> None:
    with pytest.raises(g.InvalidUploadError):
        _inspect(_png_with_header(0, 10, zlib.compress(b"")), "a.png")


# ---------------------------------------------------------------------------
# 4. Decompression bombs: small file, huge dimensions -> 422, never decoded
# ---------------------------------------------------------------------------


def test_png_decompression_bomb_is_rejected_from_its_header() -> None:
    # 50 000 x 50 000 = 2.5 GP declared in a file of a few hundred bytes.
    bomb = _png_with_header(50_000, 50_000, zlib.compress(b"\x00" * 50_001 * 4, 9))
    assert len(bomb) < 2_000
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(bomb, "bomb.png")
    assert "too large" in exc.value.message


def test_pixel_count_cap_applies_below_the_side_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "UPLOAD_MAX_IMAGE_PIXELS", 1_000)
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(_png((40, 40)), "a.png")  # 1 600 px, both sides fine
    assert "megapixels" in exc.value.message


def test_jpeg_bomb_dimensions_are_rejected() -> None:
    data = bytearray(_jpeg((16, 16)))
    sof = data.index(b"\xff\xc0")
    data[sof + 5 : sof + 9] = struct.pack(">HH", 60_000, 60_000)
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(bytes(data), "a.jpg")
    assert "too large" in exc.value.message


# Decode-check memory: a small file can declare an image whose DECODE (not its
# pixel count alone) is expensive — a progressive or multi-scan JPEG buffers
# every DCT coefficient even at 1/8 scale, a WebP decodes at full size.  The
# check estimates that cost from the header and skips the decode above budget.

# Peak RSS is read from /proc (VmHWM), reset to the current RSS just before the
# call: ru_maxrss would carry the parent's high-water mark across fork + exec
# and hide the growth.
_RSS_PROBE = """
import sys
import PIL.Image
from backend.app.modules.receipt import upload_guard as g

def kb(field):
    for line in open("/proc/self/status"):
        if line.startswith(field + ":"):
            return int(line.split()[1])
    raise SystemExit("no " + field)

data = open(sys.argv[1], "rb").read()
with open("/proc/self/clear_refs", "w") as f:
    f.write("5")
base = kb("VmHWM")
try:
    g.inspect_upload(data, filename=None, content_type=None)
    outcome = "accepted"
except g.InvalidUploadError:
    outcome = "refused"
print(outcome, (kb("VmHWM") - base) // 1024)
"""


def _peak_rss_growth_mb(path: Path) -> tuple[str, int]:
    """(outcome, peak RSS growth in MB) of inspect_upload in a fresh process."""
    if not Path("/proc/self/clear_refs").exists():
        pytest.skip("needs Linux /proc to measure peak RSS")
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # noqa: S603 — fixed interpreter + inline script
        [sys.executable, "-c", _RSS_PROBE, str(path)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    outcome, grown = proc.stdout.split()[-2:]
    return outcome, int(grown)


@pytest.mark.parametrize(
    ("name", "mode", "size", "fmt", "options"),
    [
        # 40 MP progressive 4:2:0 JPEG, a few hundred KB: 120 MB of
        # coefficients if decoded, even at 1/8 scale.
        ("prog.jpg", "RGB", (8_000, 5_000), "JPEG", {"progressive": True}),
        # 6 300 x 6 300 progressive CMYK: 320 MB of coefficients if decoded.
        ("cmyk.jpg", "CMYK", (6_300, 6_300), "JPEG", {"progressive": True}),
        # 16 MP WebP, ~30 KB: ~245 MB through Pillow's WebP decoder.
        ("big.webp", "RGB", (4_000, 4_000), "WEBP", {}),
    ],
)
def test_decode_check_stays_within_its_memory_budget(
    tmp_path: Path,
    name: str,
    mode: str,
    size: tuple[int, int],
    fmt: str,
    options: dict[str, Any],
) -> None:
    path = tmp_path / name
    Image.new(mode, size, 200 if mode == "L" else (200,) * len(mode)).save(path, fmt, **options)
    assert path.stat().st_size < 1_000_000
    outcome, grown_mb = _peak_rss_growth_mb(path)
    assert outcome == "accepted"
    budget_mb = g.DECODE_BUDGET_BYTES // (1024 * 1024)
    assert grown_mb <= budget_mb + 16, f"{name}: peak RSS grew {grown_mb} MB"


def _record_decodes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    seen: list[tuple[int, int]] = []
    real = g._decode_check

    def spy(data: bytes, size: tuple[int, int], *, reduce: bool) -> None:
        seen.append(size)
        real(data, size, reduce=reduce)

    monkeypatch.setattr(g, "_decode_check", spy)
    return seen


def _sof_of(data: bytes) -> tuple[int, bytes, list[int]]:
    """(SOF marker, SOF segment body, Ns of every scan) of a Pillow JPEG."""
    for marker in (0xC0, 0xC2):
        at = data.find(bytes((0xFF, marker)))
        if at > 0:
            (length,) = struct.unpack(">H", data[at + 2 : at + 4])
            scans, pos = [], 0
            while (pos := data.find(b"\xff\xda", pos) + 1) > 0:
                scans.append(data[pos + 3])
            return marker, data[at + 4 : at + 2 + length], scans
    raise AssertionError("no SOF")


def test_jpeg_decode_cost_estimate() -> None:
    # Baseline, one interleaved scan, decoded at 1/8: cheap whatever the size.
    marker, sof, scans = _sof_of(_jpeg((4_000, 3_000)))
    assert (marker, scans) == (0xC0, [3])
    assert g._jpeg_decode_cost(marker, sof, scans) < 2 * 1024 * 1024
    # The SAME frame with one scan per component (non-interleaved baseline)
    # buffers every coefficient: Y 4000x3000 + Cb, Cr 2000x1500 (4:2:0), 2 bytes
    # each, rounded up to whole 16x16 MCUs.
    assert 36_000_000 <= g._jpeg_decode_cost(marker, sof, [1, 1, 1]) <= 36_200_000
    # ...and so does ONE scan that carries only some components (Ns < Nf).
    assert 36_000_000 <= g._jpeg_decode_cost(marker, sof, [1]) <= 36_200_000
    # Progressive: also every coefficient.
    marker, sof, scans = _sof_of(_jpeg((4_000, 3_000), progressive=True))
    assert marker == 0xC2 and len(scans) > 1
    assert 36_000_000 <= g._jpeg_decode_cost(marker, sof, scans) <= 36_200_000
    # Greyscale progressive 8000x5000: one component.
    buf = io.BytesIO()
    Image.new("L", (8_000, 5_000), 90).save(buf, "JPEG", progressive=True)
    marker, sof, scans = _sof_of(buf.getvalue())
    assert g._jpeg_decode_cost(marker, sof, scans) == 8_000 * 5_000 * 2


def test_affordable_images_are_still_decode_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_decodes(monkeypatch)
    _inspect(_jpeg((2_000, 1_500), progressive=True), "a.jpg")  # 9 MB of coefficients
    _inspect(_jpeg((4_000, 3_000)), "a.jpg")  # baseline, 1/8 scale
    _inspect(_webp((1_000, 1_000)), "a.webp")
    assert seen == [(2_000, 1_500), (4_000, 3_000), (1_000, 1_000)]


def test_over_budget_images_skip_the_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_decodes(monkeypatch)
    monkeypatch.setattr(g, "DECODE_BUDGET_BYTES", 1_000_000)
    assert _inspect(_jpeg((2_000, 1_500), progressive=True), "a.jpg").kind == "jpeg"
    assert _inspect(_webp((1_000, 1_000)), "a.webp").kind == "webp"
    _inspect(_jpeg((2_000, 1_500)), "a.jpg")  # baseline at 1/8 is still affordable
    assert seen == [(2_000, 1_500)]


def test_one_decode_at_a_time() -> None:
    assert g._DECODE_SLOTS.acquire(blocking=False)
    try:
        assert not g._DECODE_SLOTS.acquire(blocking=False), "a second decode must wait"
    finally:
        g._DECODE_SLOTS.release()


def test_default_bomb_limits() -> None:
    s = get_settings()
    assert s.UPLOAD_MAX_IMAGE_PIXELS == 40_000_000
    assert s.UPLOAD_MAX_IMAGE_DIMENSION == 12_000
    assert s.MAX_UPLOAD_BYTES == 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# 5. Metadata stripping: GPS gone, orientation kept
# ---------------------------------------------------------------------------


def test_jpeg_exif_gps_is_removed_and_orientation_kept() -> None:
    data = _jpeg(exif=_gps_exif(6), comment=b"secret comment")
    original = Image.open(io.BytesIO(data)).getexif()
    assert original.get_ifd(0x8825), "fixture must carry GPS"

    clean = _inspect(data, "a.jpg").data
    exif = Image.open(io.BytesIO(clean)).getexif()
    assert dict(exif) == {0x0112: 6}  # ONLY the orientation survives
    assert not exif.get_ifd(0x8825)
    assert b"PhoneMaker" not in clean and b"secret comment" not in clean
    with Image.open(io.BytesIO(clean)) as img:
        img.load()  # still a valid image
        assert img.size == (64, 32)


def test_jpeg_without_rotation_gets_no_exif_at_all() -> None:
    clean = _inspect(_jpeg(exif=_gps_exif(1)), "a.jpg").data
    assert dict(Image.open(io.BytesIO(clean)).getexif()) == {}
    assert b"Exif\x00\x00" not in clean


def test_webp_exif_gps_is_removed_and_orientation_kept() -> None:
    clean = _inspect(_webp(exif=_gps_exif(8)), "a.webp").data
    exif = Image.open(io.BytesIO(clean)).getexif()
    assert dict(exif) == {0x0112: 8}
    assert b"PhoneMaker" not in clean


def test_png_text_and_exif_chunks_are_removed() -> None:
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Jane Traveller")
    info.add_itxt("Location", "40.7,-73.9")
    data = _png(pnginfo=info, exif=_gps_exif(6))
    assert b"Jane Traveller" in data
    clean = _inspect(data, "a.png").data
    assert b"Jane Traveller" not in clean and b"tEXt" not in clean and b"eXIf" not in clean
    with Image.open(io.BytesIO(clean)) as img:
        img.load()


def test_e2e_receipt_fixture_keeps_its_ocr_hint_through_the_sanitiser() -> None:
    """The CI E2E fixture steers the local OCR stub with "meal" in its PIXELS
    (a stored deflate block), which the sanitiser keeps byte-for-byte."""
    from services.ocr_provider.local import LocalOcrProvider

    fixture = Path(__file__).resolve().parents[2] / "frontend/e2e/fixtures/receipt-meal-42_50.png"
    data = fixture.read_bytes()
    clean = _inspect(data, "receipt.png", "image/png").data
    assert clean == data, "the fixture carries no metadata to strip"
    fields = LocalOcrProvider().extract(clean).fields
    assert fields["amount"] == 42.5 and fields["vendor"] == "The Capital Grille"


# ---------------------------------------------------------------------------
# 7. File names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../../etc/passwd.png", "passwd.png"),
        ("..\\..\\windows\\win.ini.png", "win.ini.png"),
        ("/abs/path/receipt.jpg", "receipt.jpg"),
        ("rece\x00ipt\r\n.png", "receipt.png"),
        ("invoice‮gnp.exe", "invoicegnp.exe"),  # RLO override removed
        ("ｒｅｃｅｉｐｔ．ｐｎｇ", "receipt.png"),  # full-width folded (NFKC)
        ("...hidden.png", "hidden.png"),
        ('a<b>c:"d|e?.png', "a_b_c__d_e_.png"),
        ("", "receipt"),
        ("../..", "receipt"),
    ],
)
def test_safe_filename(raw: str, safe: str) -> None:
    assert g.safe_filename(raw) == safe


def test_safe_filename_caps_length_and_keeps_extension() -> None:
    out = g.safe_filename("x" * 500 + ".png")
    assert len(out) == 120 and out.endswith(".png")


def test_unicode_extension_trick_is_judged_on_the_real_extension() -> None:
    # Displays as "invoiceexe.png" in a bidi-aware UI; really ends in .gnp... -> 415.
    with pytest.raises(g.UnsupportedMediaTypeError):
        _inspect(_png(), "invoice‮gnp.exe", "image/png")
    # Full-width ".ｐｎｇ" is a real .png once normalised.
    assert _inspect(_png(), "ｒｅｃｅｉｐｔ．ｐｎｇ", "image/png").kind == "png"


# ---------------------------------------------------------------------------
# 8. Bounded read in the route
# ---------------------------------------------------------------------------


class _CountingFile(io.RawIOBase):
    """A 4 MB 'file' that counts how much was read from it."""

    def __init__(self, size: int = 4 * 1024 * 1024) -> None:
        self.read_bytes = 0
        self.size = size

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        n = min(len(b), self.size - self.read_bytes)
        b[:n] = b"\x00" * n
        self.read_bytes += n
        return n


def test_read_upload_bounded_stops_one_chunk_past_the_limit() -> None:
    raw = _CountingFile()
    upload = UploadFile(file=io.BufferedReader(raw, buffer_size=1024))  # type: ignore[arg-type]
    with pytest.raises(g.PayloadTooLargeError) as exc:
        asyncio.run(g.read_upload_bounded(upload, 100_000))
    assert exc.value.status_code == 413
    assert raw.read_bytes <= 100_000 + 2 * 64 * 1024


def test_read_upload_bounded_returns_small_files_whole() -> None:
    upload = UploadFile(file=io.BytesIO(b"abc"))
    assert asyncio.run(g.read_upload_bounded(upload, 3)) == b"abc"


# ---------------------------------------------------------------------------
# 9. Body-size middleware
# ---------------------------------------------------------------------------


def _mini_app(limit: int = 1_000, upload_limit: int = 5_000) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_middleware(
        BodySizeLimitMiddleware,
        upload_paths=frozenset({"/upload"}),
        default_limit=lambda: limit,
        upload_limit=lambda: upload_limit,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIDMiddleware)
    return app


def _assert_413(resp: Any) -> None:
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert body["error"]["correlation_id"] == resp.headers["X-Correlation-ID"]


def test_body_within_limit_passes() -> None:
    client = TestClient(_mini_app())
    r = client.post("/echo", content=b"x" * 1_000)
    assert r.status_code == 200 and r.json() == {"n": 1_000}


def test_content_length_over_limit_is_413() -> None:
    _assert_413(TestClient(_mini_app()).post("/echo", content=b"x" * 1_001))


def test_streamed_body_without_content_length_is_413() -> None:
    def gen() -> Iterator[bytes]:
        for _ in range(50):
            yield b"x" * 100

    _assert_413(TestClient(_mini_app()).post("/echo", content=gen()))


def test_upload_path_gets_the_larger_limit() -> None:
    client = TestClient(_mini_app(limit=1_000, upload_limit=5_000))
    assert client.post("/upload", content=b"x" * 5_000).status_code == 200
    assert client.post("/echo", content=b"x" * 5_000).status_code == 413
    _assert_413(client.post("/upload", content=b"x" * (5_000 + MULTIPART_OVERHEAD_BYTES + 1)))


def test_nosniff_on_every_response() -> None:
    client = TestClient(_mini_app())
    assert client.post("/echo", content=b"x").headers["X-Content-Type-Options"] == "nosniff"
    assert client.post("/echo", content=b"x" * 5_000).headers["X-Content-Type-Options"] == "nosniff"


def _run_asgi(
    app: Any, scope_headers: list[tuple[bytes, bytes]], chunks: Iterator[bytes]
) -> tuple[list[dict], dict[str, int]]:
    """Drive *app* with a streamed body, counting how many body bytes were pulled."""
    stats = {"pulled": 0}
    sent: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "root_path": "",
        "headers": scope_headers,
        "client": ("test", 1),
        "server": ("test", 80),
    }

    async def receive() -> dict:
        chunk = next(chunks, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        stats["pulled"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent, stats


def test_lying_content_length_cannot_make_the_app_buffer_past_the_limit() -> None:
    """Declares 10 bytes, streams 1 MB: at most limit + one chunk is ever pulled."""
    chunk = 512

    def endless() -> Iterator[bytes]:
        for _ in range(2_048):  # 1 MiB
            yield b"x" * chunk

    sent, stats = _run_asgi(_mini_app(limit=4_096), [(b"content-length", b"10")], endless())
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
    assert stats["pulled"] <= 4_096 + chunk


def test_missing_content_length_generator_body_is_bounded() -> None:
    chunk = 1_000

    def endless() -> Iterator[bytes]:
        while True:
            yield b"y" * chunk

    sent, stats = _run_asgi(_mini_app(limit=10_000), [], endless())
    assert sent[0]["status"] == 413
    assert stats["pulled"] <= 10_000 + chunk


def test_content_length_reject_never_reads_the_body() -> None:
    def must_not_read() -> Iterator[bytes]:
        raise AssertionError("body must not be read")
        yield b""  # pragma: no cover

    sent, stats = _run_asgi(_mini_app(limit=100), [(b"content-length", b"101")], must_not_read())
    assert sent[0]["status"] == 413 and stats["pulled"] == 0


# ---------------------------------------------------------------------------
# 10. The real app: middleware is installed, before auth and before the route
# ---------------------------------------------------------------------------


def test_real_app_rejects_oversized_receipt_upload_before_auth() -> None:
    client = TestClient(real_app, raise_server_exceptions=False)
    limit = get_settings().MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES
    r = client.post(
        "/api/v1/receipts",
        content=b"x" * (limit + 1),
        headers={"Content-Type": "multipart/form-data; boundary=zzz"},
    )
    _assert_413(r)
    assert "10 MB" in r.json()["error"]["message"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_real_app_caps_json_bodies() -> None:
    client = TestClient(real_app, raise_server_exceptions=False)
    r = client.post(
        "/api/v1/auth/login",
        content=b"{" + b" " * get_settings().MAX_REQUEST_BODY_BYTES + b"}",
        headers={"Content-Type": "application/json"},
    )
    _assert_413(r)


# ---------------------------------------------------------------------------
# 11. Presigned links pin the served type and disposition
# ---------------------------------------------------------------------------


def test_served_headers_for_new_keys() -> None:
    assert served_headers_for_key("org/abc.png") == {
        "ResponseContentType": "image/png",
        "ResponseContentDisposition": 'inline; filename="receipt.png"',
    }
    assert served_headers_for_key("org/abc.jpg")["ResponseContentType"] == "image/jpeg"
    # PDF receipts are no longer accepted, so no .pdf key is ever generated.
    assert served_headers_for_key("org/abc.pdf") == {}
    # Pre-phase-11 keys (no extension) keep the stored object's type.
    assert served_headers_for_key("org/3f2c0d1e-uuid") == {}
    assert served_headers_for_key("receipts/seed/x.html") == {}


# ---------------------------------------------------------------------------
# 12. Review fixes: dependencies, guards proven by tests
# ---------------------------------------------------------------------------


def test_pillow_is_a_declared_runtime_dependency() -> None:
    """The decode check imports Pillow at runtime: the production image (built
    from backend/requirements.txt) and CI must install it."""
    import re

    repo = Path(__file__).resolve().parents[2]
    reqs = (repo / "backend" / "requirements.txt").read_text().splitlines()
    assert any(re.match(r"(?i)pillow\s*[<>=~]", line.strip()) for line in reqs)
    dockerfile = (repo / "docker" / "Dockerfile.backend").read_text()
    assert "backend/requirements.txt" in dockerfile


def test_inspect_holds_the_decode_slot_around_image_work(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class _Slot:
        def __enter__(self) -> None:
            events.append("acquire")

        def __exit__(self, *exc: object) -> None:
            events.append("release")

    real = g._decode_check

    def spy(data: bytes, size: tuple[int, int], *, reduce: bool) -> None:
        events.append("decode")
        real(data, size, reduce=reduce)

    monkeypatch.setattr(g, "_DECODE_SLOTS", _Slot())
    monkeypatch.setattr(g, "_decode_check", spy)
    _inspect(_jpeg(), "a.jpg")
    assert events == ["acquire", "decode", "release"]


@pytest.mark.parametrize(("w", "h"), [(0, 5), (5, 0), (0, 0)])
def test_image_limits_refuse_zero_dimensions(w: int, h: int) -> None:
    limits = g._ImageLimits(max_pixels=1_000_000, max_dimension=1_000)
    with pytest.raises(g.InvalidUploadError) as exc:
        limits.check(w, h)
    assert "no pixels" in exc.value.message


def test_bytes_after_the_webp_riff_end_are_dropped() -> None:
    data = _webp()
    clean = _inspect(data + b"<script>polyglot</script>", "a.webp").data
    assert b"polyglot" not in clean
    assert clean == _inspect(data, "a.webp").data


def test_png_pixel_overflow_stops_at_the_first_oversized_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IDAT stream larger than the header allows is refused as soon as it
    overflows: the remaining chunks are never decompressed."""
    calls = {"n": 0}
    real_decompressobj = zlib.decompressobj

    class _Counting:
        def __init__(self) -> None:
            self._d = real_decompressobj()

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            calls["n"] += 1
            return self._d.decompress(data, max_length)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._d, name)

    class _Zlib:
        def __getattr__(self, name: str) -> Any:
            return getattr(zlib, name)

        decompressobj = staticmethod(_Counting)

    # 4x4 greyscale needs 20 raw bytes; the first IDAT alone inflates to 2 000.
    raw = zlib.compress(b"\x00" * 2_000)
    first, rest = raw[:-6], raw[-6:]
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 0, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", first)
        + b"".join(_chunk(b"IDAT", rest[i : i + 1]) for i in range(len(rest)))
        + b"".join(_chunk(b"IDAT", b"") for _ in range(50))
        + _chunk(b"IEND", b"")
    )
    monkeypatch.setattr(g, "zlib", _Zlib())
    with pytest.raises(g.InvalidUploadError):
        _inspect(png, "a.png")
    assert calls["n"] <= 2


def test_over_limit_body_is_never_pulled_again_even_if_the_app_retries() -> None:
    """An app that swallows the 413 and keeps calling receive() gets nothing more."""

    async def greedy(scope: Any, receive: Any, send: Any) -> None:
        for _ in range(200):
            try:
                await receive()
            except StarletteHTTPException:
                continue
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    chunk = 500

    def endless() -> Iterator[bytes]:
        while True:
            yield b"z" * chunk

    app = BodySizeLimitMiddleware(greedy, default_limit=lambda: 1_000, upload_limit=lambda: 1_000)
    _sent, stats = _run_asgi(app, [], endless())
    assert stats["pulled"] <= 1_000 + chunk


# ---------------------------------------------------------------------------
# 13. PDF receipts (phase 12: rasterise-only, see test_pdf_receipts.py).  The
#     phase-11 refusal is replaced on purpose: a PDF is rendered to ONE PNG in
#     a contained child and only the PNG is kept; a claimed / named PDF must
#     carry PDF bytes; an image that is also a PDF in the reader's window is an
#     ambiguous polyglot and refused; an image with a PDF tail past the window
#     is stored as the image only.
# ---------------------------------------------------------------------------


def _noisy(make: Any) -> bytes:
    """A real image of ``make``'s format whose encoding is several KiB (noise)."""
    fmt = "PNG" if make is _png else "JPEG"
    img = Image.frombytes("RGB", (64, 64), random.Random(7).randbytes(64 * 64 * 3))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _real_pdf() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 200, 200)).save(buf, "PDF")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("data", "name", "ctype"),
    [
        pytest.param(_real_pdf(), "receipt.pdf", "application/pdf", id="honest-pdf"),
        pytest.param(_real_pdf(), None, None, id="magic-only"),
        pytest.param(_real_pdf(), None, "application/octet-stream", id="octet-stream"),
        pytest.param(_real_pdf(), "scan.PDF", "application/x-pdf", id="x-pdf-upper-ext"),
        pytest.param(b"\x00" * 300 + _real_pdf(), None, None, id="header-inside-reader-window"),
    ],
)
def test_pdf_receipts_are_rasterised_to_a_png(
    data: bytes, name: str | None, ctype: str | None
) -> None:
    checked = _inspect(data, name, ctype)
    assert checked.kind == "png"
    assert checked.content_type == "image/png"
    assert checked.source == "pdf"
    assert checked.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"%PDF" not in checked.data


@pytest.mark.parametrize(
    ("data", "name", "ctype"),
    [
        pytest.param(_real_pdf(), "scan.png", "image/png", id="pdf-disguised-as-png"),
        pytest.param(_real_pdf(), None, "image/jpeg", id="pdf-claimed-jpeg"),
        pytest.param(_real_pdf(), "receipt.pdf", "text/plain", id="pdf-claimed-text"),
        pytest.param(_png(), "receipt.pdf", None, id="png-named-pdf"),
        pytest.param(_png(), "receipt.PDF", "image/png", id="png-named-pdf-upper"),
        pytest.param(_png(), None, "application/pdf", id="png-claimed-pdf"),
        pytest.param(_jpeg(), "r.jpg", "application/x-pdf", id="jpeg-claimed-x-pdf"),
    ],
)
def test_a_pdf_and_its_claimed_type_or_name_must_agree(
    data: bytes, name: str | None, ctype: str | None
) -> None:
    with pytest.raises(g.UnsupportedMediaTypeError) as exc:
        _inspect(data, name, ctype)
    assert exc.value.status_code == 415
    assert exc.value.code == "UNSUPPORTED_MEDIA_TYPE"


def test_jpeg_carrying_a_pdf_header_in_its_first_kilobyte_is_refused() -> None:
    """A PDF reader looks for ``%PDF-`` anywhere in the first 1024 bytes, so an
    image whose early segments carry one is a PDF polyglot: ambiguous, refused."""
    jpeg = _jpeg()
    com = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n"
    data = jpeg[:2] + b"\xff\xfe" + struct.pack(">H", len(com) + 2) + com + jpeg[2:]
    with pytest.raises(g.UnsupportedMediaTypeError) as exc:
        _inspect(data, "r.jpg", "image/jpeg")
    assert exc.value.message == g.POLYGLOT_MESSAGE


@pytest.mark.parametrize("make", [_png, _jpeg], ids=["png", "jpeg"])
def test_image_with_a_pdf_tail_is_stored_as_the_image_only(make: Any) -> None:
    """Image header + a whole PDF appended past the reader's 1 KiB header window:
    the real decoder accepts the image, the tail is dropped, and the result is
    stored as the IMAGE type — never as a PDF, and no ``%PDF`` survives."""
    image = _noisy(make)
    assert len(image) > 2048
    checked = _inspect(image + b"\n" + _real_pdf(), None, None)
    assert checked.kind in {"png", "jpeg"}
    assert checked.source == "image"
    assert checked.content_type.startswith("image/")
    assert b"%PDF" not in checked.data
    with Image.open(io.BytesIO(checked.data)) as img:
        img.load()  # the real decoder accepts what is stored


@pytest.mark.parametrize("make", [_png, _jpeg], ids=["png", "jpeg"])
def test_small_image_with_a_pdf_inside_the_header_window_is_refused(make: Any) -> None:
    """A tiny image + PDF whose header lands in the first 1 KiB IS a PDF to a
    reader AND an image to a decoder: ambiguous, refused, not stored."""
    with pytest.raises(g.UnsupportedMediaTypeError) as exc:
        _inspect(make() + b"\n" + _real_pdf(), None, None)
    assert exc.value.message == g.POLYGLOT_MESSAGE


def test_pdf_is_a_source_not_a_stored_kind() -> None:
    """A PDF is never a stored / served type: only its rendered PNG is."""
    assert "pdf" not in g.CONTENT_TYPES and "pdf" not in g.EXTENSIONS
    for removed in ("KIND_PDF", "_check_pdf", "_pdf_streams", "_inflate_object_stream"):
        assert not hasattr(g, removed), removed
    assert get_settings().UPLOAD_PDF_MAX_PAGES == 3
    assert "PDF" in g.SUPPORTED_TYPES_MESSAGE


# ---------------------------------------------------------------------------
# 14. Integration review fix (f): the JPEG decode-memory estimate must not
#     differ from what libjpeg / Pillow will actually do.  Each SOS is parsed
#     (Ns against the frame's component count); a progressive, multi-scan,
#     hierarchical, lossless or partial-scan frame gets the WORST-CASE cost
#     (every DCT coefficient: blocks x 128 bytes); more than one SOF is refused.
# ---------------------------------------------------------------------------


def _segment_at(data: bytes, marker: int) -> tuple[int, int]:
    """(offset of the 0xFF marker, total length incl. marker) of the first segment."""
    at = data.find(bytes((0xFF, marker)))
    assert at > 0, hex(marker)
    (length,) = struct.unpack(">H", data[at + 2 : at + 4])
    return at, 2 + length


def _with_sos(data: bytes, body: bytes) -> bytes:
    """Replace the (first) SOS header's body, keeping the entropy-coded data."""
    at, total = _segment_at(data, 0xDA)
    return data[:at] + b"\xff\xda" + struct.pack(">H", len(body) + 2) + body + data[at + total :]


def _sos_body(data: bytes) -> bytes:
    at, total = _segment_at(data, 0xDA)
    return data[at + 4 : at + total]


_BIG = (4_000, 3_000)  # baseline 4:2:0: ~0.3 MB at 1/8 scale, ~36 MB worst case


def test_jpeg_with_more_than_one_sof_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # No decode check (budget 0): the STRUCTURE rule alone must refuse it.
    monkeypatch.setattr(g, "DECODE_BUDGET_BYTES", 0)
    data = _jpeg((64, 32))
    at, total = _segment_at(data, 0xC0)
    sof = data[at : at + total]
    for twice in (data[: at + total] + sof + data[at + total :], data[:at] + sof + data[at:]):
        with pytest.raises(g.InvalidUploadError):
            _inspect(twice, "a.jpg")
    # A second SOF of ANOTHER type (e.g. a hierarchical frame header) too.
    other = b"\xff\xc5" + sof[2:]
    with pytest.raises(g.InvalidUploadError):
        _inspect(data[: at + total] + other + data[at + total :], "a.jpg")


def test_partial_scan_baseline_jpeg_is_costed_worst_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """A baseline frame whose FIRST scan carries fewer components than the frame
    (Ns < Nf) makes libjpeg buffer every coefficient even with ONE SOS: it must
    not be decoded on the cheap 1/8-scale estimate."""
    seen = _record_decodes(monkeypatch)
    monkeypatch.setattr(g, "DECODE_BUDGET_BYTES", 10 * 1024 * 1024)
    data = _jpeg(_BIG)
    _inspect(data, "a.jpg")  # control: the full interleaved scan IS decode-checked
    assert seen == [_BIG]
    body = _sos_body(data)
    assert body[0] == 3
    partial = _with_sos(data, b"\x01" + body[1:3] + body[7:])  # Ns=1: only Y
    seen.clear()
    assert _inspect(partial, "a.jpg").kind == "jpeg"
    assert seen == []  # over budget at the worst-case cost: never decoded


@pytest.mark.parametrize("sof", [0xC3, 0xC5, 0xC6, 0xC7, 0xCB, 0xCD, 0xCE, 0xCF])
def test_lossless_and_hierarchical_frames_are_costed_worst_case(
    sof: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_decodes(monkeypatch)
    monkeypatch.setattr(g, "DECODE_BUDGET_BYTES", 10 * 1024 * 1024)
    data = _jpeg(_BIG)
    at, _total = _segment_at(data, 0xC0)
    crafted = data[: at + 1] + bytes((sof,)) + data[at + 2 :]
    try:
        _inspect(crafted, "a.jpg")
    except g.InvalidUploadError:
        pass  # refusing is fine too; decoding on the cheap estimate is not
    assert seen == []


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda b: b"\x00" + b[1:], id="ns-zero"),
        pytest.param(lambda b: b"\x05" + b[1:], id="ns-five"),
        pytest.param(lambda b: b"\x04" + b[1:], id="ns-more-than-the-frame"),
        pytest.param(lambda b: b[:-1], id="header-too-short"),
        pytest.param(lambda b: b + b"\x00", id="header-too-long"),
        pytest.param(lambda b: b[:1] + b"\x09" + b[2:], id="unknown-component"),
        pytest.param(lambda b: b[:1] + b[1:3] + b[1:3] + b[5:], id="duplicate-component"),
    ],
)
def test_malformed_scan_headers_are_rejected(mangle: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "DECODE_BUDGET_BYTES", 0)  # the structure rule alone decides
    data = _jpeg((64, 32))
    with pytest.raises(g.InvalidUploadError):
        _inspect(_with_sos(data, mangle(_sos_body(data))), "a.jpg")
