"""Hand-built PDFs for the phase-12 PDF receipt tests (no PDF library needed).

``build_pdf`` writes a classic PDF (header, numbered objects, a correct xref
table and trailer).  ``text_pdf`` makes an n-page text receipt; the active-
content builders add JavaScript / OpenAction / Launch / embedded files / XFA
on top of a valid one-page receipt, each carrying a unique marker string so a
test can prove nothing of it survives rasterisation.
"""

from __future__ import annotations

import io
import zlib

from PIL import Image

MARK_JS = b"ATLAS-JS-MARKER"
MARK_LAUNCH = b"ATLAS-LAUNCH-MARKER"
MARK_EMBEDDED = b"ATLAS-EMBEDDED-MARKER"
MARK_XFA = b"ATLAS-XFA-MARKER"


def stream(entries: bytes, data: bytes) -> bytes:
    return b"<< " + entries + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def build_pdf(objs: dict[int, bytes], *, root: int = 1, trailer_extra: bytes = b"") -> bytes:
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    size = max(objs) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for n in range(1, size):
        out += b"%010d 00000 n \n" % offsets[n] if n in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root %d 0 R " % (size, root) + trailer_extra + b" >>\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def text_pdf(
    pages: int = 1,
    *,
    mediabox: tuple[float, float, float, float] = (0, 0, 612, 792),
    catalog_extra: bytes = b"",
    page_extra: bytes = b"",
    extra_objs: dict[int, bytes] | None = None,
    trailer_extra: bytes = b"",
    content: bytes = b"BT /F1 28 Tf 72 700 Td (COFFEE HOUSE  TOTAL 12.50 USD) Tj ET",
) -> bytes:
    """An n-page text receipt; page objects are numbered from 100."""
    box = b"[%s]" % b" ".join(b"%g" % v for v in mediabox)
    kids = b" ".join(b"%d 0 R" % (100 + i) for i in range(pages))
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R " + catalog_extra + b" >>",
        2: b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages,
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: stream(b"", content),
    }
    for i in range(pages):
        objs[100 + i] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox "
            + box
            + b" /Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R "
            + page_extra
            + b" >>"
        )
    objs.update(extra_objs or {})
    return build_pdf(objs, trailer_extra=trailer_extra)


def pillow_pdf(pages: int = 1, size: tuple[int, int] = (612, 792)) -> bytes:
    """A PDF written by Pillow (one image per page)."""
    imgs = [Image.new("RGB", size, (255, 255, 255 - 40 * i)) for i in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def js_openaction_pdf() -> bytes:
    action = b"<< /S /JavaScript /JS (app.alert\\('" + MARK_JS + b"'\\);) >>"
    return text_pdf(
        catalog_extra=b"/OpenAction 5 0 R /Names << /JavaScript << /Names [(x) 6 0 R] >> >>",
        page_extra=b"/AA << /O 5 0 R >>",
        extra_objs={5: action, 6: action},
    )


def launch_pdf() -> bytes:
    return text_pdf(
        catalog_extra=b"/OpenAction 5 0 R",
        extra_objs={5: b"<< /S /Launch /F (" + MARK_LAUNCH + b".exe) /NewWindow true >>"},
    )


def embedded_file_pdf() -> bytes:
    return text_pdf(
        catalog_extra=b"/Names << /EmbeddedFiles << /Names [(evil.exe) 6 0 R] >> >>",
        extra_objs={
            6: b"<< /Type /Filespec /F (evil.exe) /EF << /F 7 0 R >> >>",
            7: stream(b"/Type /EmbeddedFile", b"MZ" + MARK_EMBEDDED * 4),
        },
    )


def xfa_pdf() -> bytes:
    xdp = (
        b'<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/"><script>'
        + MARK_XFA
        + b"</script></xdp:xdp>"
    )
    return text_pdf(
        catalog_extra=b"/AcroForm << /Fields [] /XFA 8 0 R /NeedsRendering true >>",
        extra_objs={8: stream(b"", xdp)},
    )


def encrypted_pdf() -> bytes:
    """A PDF behind the standard security handler with a (non-empty) user password."""
    o = b"<" + b"ab" * 32 + b">"
    u = b"<" + b"cd" * 32 + b">"
    file_id = b"<00112233445566778899aabbccddeeff>"
    return text_pdf(
        extra_objs={9: b"<< /Filter /Standard /V 1 /R 2 /O " + o + b" /U " + u + b" /P -4 >>"},
        trailer_extra=b"/Encrypt 9 0 R /ID [" + file_id + b" " + file_id + b"]",
    )


def image_bomb_pdf(side: int = 20_000) -> bytes:
    """A small page drawing a side x side greyscale image (side^2 bytes inflated)."""
    raw = zlib.compressobj(9)
    row = b"\x00" * side
    chunks = [raw.compress(row) for _ in range(side)]
    data = b"".join(chunks) + raw.flush()
    img = stream(
        b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter /FlateDecode" % (side, side),
        data,
    )
    return text_pdf(
        mediabox=(0, 0, 200, 200),
        content=b"q 200 0 0 200 0 0 cm /Im1 Do Q",
        page_extra=b"",
        extra_objs={
            5: img,
            100: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
        },
    )
