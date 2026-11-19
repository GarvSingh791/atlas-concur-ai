"""PDF rasteriser CHILD process (phase 12) — never imported by the app.

Run by ``pdf_raster.rasterize_pdf`` as ``python -I -B <this file> <json limits>``
in a short-lived child, with the PDF bytes on stdin.  It writes ONE framed
answer to stdout: either a single PNG (the first pages stacked vertically) or an
error code.  The parent never trusts anything else about this process: it
bounds the wall clock and the size of what it reads, and kills the process
group afterwards.

Order matters — the limits that do not disturb the (trusted) imports are set
first, the rest right before the untrusted bytes are parsed:

1. ``RLIMIT_CPU`` (CPU seconds; SIGXCPU then SIGKILL), ``RLIMIT_CORE`` = 0 and
   ``RLIMIT_FSIZE`` = 0 (no file can be written; pipes are not files).
2. Import Pillow and pypdfium2 (trusted code).
3. Read stdin (bounded).
4. ``RLIMIT_AS`` = the address space already mapped + the memory budget.
   PDFium reserves a large, mostly untouched address range when it loads
   (~400 MB of VmSize), so an absolute 256 MB cap would stop the import itself;
   the budget is therefore what parsing and rendering may ADD.
   ``RLIMIT_NOFILE`` = 0: stdin/stdout/stderr stay usable, but no new
   descriptor — no socket, no file — can be created, not even by closing one
   of the three first (a limit of 3 would let fd 0 be reused).
5. Parse, check page count / sizes (before any page is rendered), render.

Forms are never initialised (no ``init_forms``), so no form / XFA / JavaScript
environment exists; the pypdfium2 PDFium build has no V8 or XFA anyway.
"""

from __future__ import annotations

import io
import json
import math
import resource
import struct
import sys
from typing import Any

MAGIC = b"ATPDF1"

ERR_ENCRYPTED = "encrypted"
ERR_UNREADABLE = "unreadable"
ERR_NO_PAGES = "no_pages"
ERR_PAGE_TOO_LARGE = "page_too_large"
ERR_TOO_LARGE_OUTPUT = "too_large_output"
ERR_TOO_LARGE_INPUT = "too_large_input"

# Gap between stacked pages (px) and its colour.
_PAGE_GAP = 12
_GAP_COLOUR = (220, 220, 220)
# Each re-encode attempt when the PNG is over the output cap shrinks by this.
_SHRINK = 0.7
_SHRINK_ATTEMPTS = 4


def emit(header: dict[str, Any], body: bytes = b"") -> None:
    """Write one framed answer: MAGIC, header length, JSON header, body."""
    raw = json.dumps(header, separators=(",", ":")).encode("ascii")
    out = sys.stdout.buffer
    out.write(MAGIC + struct.pack(">I", len(raw)) + raw + body)
    out.flush()


def apply_early_limits(cpu_seconds: int) -> None:
    """Limits that are safe to set before the imports."""
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def mapped_bytes() -> int:
    """The address space this process has mapped now (Linux), or 0 when unknown."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def apply_late_limits(memory_bytes: int) -> None:
    """Memory growth cap and no new file descriptors (set just before parsing)."""
    base = mapped_bytes()
    cap = base + memory_bytes
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    resource.setrlimit(resource.RLIMIT_NOFILE, (0, 0))


def read_input(limit: int) -> bytes | None:
    data = sys.stdin.buffer.read(limit + 1)
    return None if len(data) > limit else data


def _scale_for(width_pt: float, height_pt: float, max_side_px: int, max_scale: float) -> float:
    return min(max_side_px / max(width_pt, height_pt), max_scale)


def render(data: bytes, opts: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """(header, png) for *data*; header carries ``error`` on failure."""
    import pypdfium2 as pdfium  # noqa: PLC0415 — imported before the limits by main()
    from PIL import Image  # noqa: PLC0415

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            return {"error": ERR_ENCRYPTED}, b""
        return {"error": ERR_UNREADABLE}, b""

    total = len(pdf)
    if total <= 0:
        return {"error": ERR_NO_PAGES}, b""
    used = min(total, int(opts["max_pages"]))
    max_pt = float(opts["max_page_points"])

    # Every page that will be rendered is size-checked BEFORE any is rendered.
    pages = []
    for i in range(used):
        page = pdf[i]
        width, height = page.get_size()
        left, bottom, right, top = page.get_mediabox()
        sides = (width, height, right - left, top - bottom)
        if not all(math.isfinite(s) for s in sides):
            return {"error": ERR_UNREADABLE}, b""
        if min(width, height) <= 0 or min(abs(right - left), abs(top - bottom)) <= 0:
            return {"error": ERR_NO_PAGES}, b""
        if max(abs(s) for s in sides) > max_pt:
            return {"error": ERR_PAGE_TOO_LARGE}, b""
        pages.append((page, width, height))

    images = []
    for page, width, height in pages:
        scale = _scale_for(width, height, int(opts["max_side_px"]), float(opts["max_scale"]))
        bitmap = page.render(scale=scale, may_draw_forms=False)
        images.append(bitmap.to_pil().convert("RGB"))
        del bitmap

    canvas_w = max(img.width for img in images)
    canvas_h = sum(img.height for img in images) + _PAGE_GAP * (len(images) - 1)
    canvas = Image.new("RGB", (canvas_w, canvas_h), _GAP_COLOUR)
    y = 0
    for img in images:
        # Narrower pages are centred on a white strip of the full width.
        strip = Image.new("RGB", (canvas_w, img.height), (255, 255, 255))
        strip.paste(img, ((canvas_w - img.width) // 2, 0))
        canvas.paste(strip, (0, y))
        y += img.height + _PAGE_GAP
    del images

    max_out = int(opts["max_output_bytes"])
    for _ in range(_SHRINK_ATTEMPTS):
        buf = io.BytesIO()
        canvas.save(buf, "PNG", compress_level=6)
        png = buf.getvalue()
        if len(png) <= max_out:
            return {"pages_total": total, "pages_used": used}, png
        canvas = canvas.resize(
            (max(1, int(canvas.width * _SHRINK)), max(1, int(canvas.height * _SHRINK)))
        )
    return {"error": ERR_TOO_LARGE_OUTPUT}, b""


def main(argv: list[str]) -> int:
    opts = json.loads(argv[1])
    apply_early_limits(int(opts["cpu_seconds"]))
    import pypdfium2  # noqa: F401, PLC0415 — trusted imports before the caps
    from PIL import Image  # noqa: PLC0415

    # Pillow imports its format plugins lazily on the first save; after
    # RLIMIT_NOFILE no module file could be opened, so load them now.
    Image.preinit()

    data = read_input(int(opts["max_input_bytes"]))
    if data is None:
        emit({"error": ERR_TOO_LARGE_INPUT})
        return 0
    apply_late_limits(int(opts["memory_bytes"]))
    try:
        header, png = render(data, opts)
    except MemoryError:
        header, png = {"error": ERR_UNREADABLE}, b""
    except Exception:  # noqa: BLE001 — any parser failure is an unreadable PDF
        header, png = {"error": ERR_UNREADABLE}, b""
    emit(header, png)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
