"""Receipt upload guard — size, type, structure and metadata (phase 11).

Everything here runs BEFORE a byte is stored or sent to OCR:

1. ``read_upload_bounded`` reads the multipart file in chunks and stops at
   ``MAX_UPLOAD_BYTES + 1`` (413 PAYLOAD_TOO_LARGE) — a second line behind the
   request-body middleware.
2. ``inspect_upload`` decides the type from the CONTENT (magic bytes) against an
   allow-list (JPEG, PNG, WebP).  The client's ``Content-Type`` and the file
   name's extension may be absent, but when present they must agree with the
   sniffed type (415 UNSUPPORTED_MEDIA_TYPE).  Empty, truncated, corrupt or
   zero-sized files are 422 INVALID_UPLOAD.
3. Images are checked structurally from their headers (dimensions, pixel count
   — the decompression-bomb guard) without decoding the full image, then
   SANITISED losslessly: metadata (EXIF incl. GPS, XMP, text chunks, comments)
   and any bytes after the image's end marker (polyglot payloads) are dropped.
   The pixels are not re-encoded, so OCR sees exactly what was photographed.  A
   JPEG/WebP EXIF orientation is preserved by writing back a minimal EXIF block
   holding ONLY the orientation tag.  One decode check proves the image is
   readable — only when its estimated decode memory fits
   ``DECODE_BUDGET_BYTES``, one image at a time per process.
4. PDF receipts are RASTERISED, never stored (phase 12; ``pdf_raster.py``).
   A file is a PDF when its bytes carry a PDF header where a reader looks for
   one (``%PDF-`` within the first 1024 bytes); a client that CLAIMS a PDF
   type or names a ``.pdf`` must send such bytes (else 415).  The PDF is
   rendered in a contained child process into ONE PNG (the first pages
   stacked) and that PNG goes through step 3 like any upload — only the PNG is
   stored, served and sent to OCR.  Encrypted, damaged, page-less or absurdly
   large-paged PDFs, and any render failure / timeout, are 422 INVALID_UPLOAD.
   A file that is BOTH an image (by magic) and carries a PDF header in that
   window is ambiguous (a polyglot) and refused (415).  An image with a PDF
   appended after its end marker is sanitised like any image: the tail is
   dropped and it is stored as the image type only.

The stored object key is server generated (``<org>/<uuid>.<ext>``, ``ext`` from
the SNIFFED type) and the stored content type is the sniffed one.  The client
file name is never stored, logged or used in a key; ``safe_filename`` only
normalises it so its extension can be compared.
"""

from __future__ import annotations

import logging
import math
import re
import struct
import threading
import unicodedata
import warnings
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from fastapi import UploadFile

from backend.app.core.errors import AppError, ErrorCode
from config.settings import get_settings

logger = logging.getLogger(__name__)

INVALID_UPLOAD = "INVALID_UPLOAD"
UPLOAD_QUOTA_EXCEEDED = "UPLOAD_QUOTA_EXCEEDED"

_READ_CHUNK = 64 * 1024

# Allow-listed kinds: sniffed kind -> (stored content type, stored extension).
KIND_JPEG = "jpeg"
KIND_PNG = "png"
KIND_WEBP = "webp"
CONTENT_TYPES: dict[str, str] = {
    KIND_JPEG: "image/jpeg",
    KIND_PNG: "image/png",
    KIND_WEBP: "image/webp",
}
EXTENSIONS: dict[str, str] = {KIND_JPEG: "jpg", KIND_PNG: "png", KIND_WEBP: "webp"}

# Client-claimed Content-Type -> kind.  None = "the client did not say".
_CLAIMED_TYPES: dict[str, str | None] = {
    "": None,
    "application/octet-stream": None,
    "image/jpeg": KIND_JPEG,
    "image/jpg": KIND_JPEG,
    "image/pjpeg": KIND_JPEG,
    "image/png": KIND_PNG,
    "image/x-png": KIND_PNG,
    "image/webp": KIND_WEBP,
}
_EXTENSION_KINDS: dict[str, str] = {
    "jpg": KIND_JPEG,
    "jpeg": KIND_JPEG,
    "jpe": KIND_JPEG,
    "png": KIND_PNG,
    "webp": KIND_WEBP,
}

SUPPORTED_TYPES_MESSAGE = "Upload a JPEG, PNG or WebP image or a PDF of the receipt."

# PDF receipts are rasterised (see the module docstring and pdf_raster.py).
KIND_PDF_SOURCE = "pdf"
POLYGLOT_MESSAGE = (
    "This file looks like both an image and a PDF, so it can't be read safely. "
    "Upload a plain PNG, JPG or PDF of the receipt."
)
PDF_MISMATCH_MESSAGE = (
    "The file is named or declared as a PDF but its contents aren't a PDF. "
    + SUPPORTED_TYPES_MESSAGE
)
PDF_ENCRYPTED_MESSAGE = (
    "This PDF is password-protected. Remove the password (or print it to a new PDF) "
    "and upload it again."
)
PDF_UNREADABLE_MESSAGE = (
    "We couldn't read this PDF. It may be damaged — export it again, or upload a PNG "
    "or JPG of the receipt."
)
PDF_NO_PAGES_MESSAGE = "This PDF has no pages to read."
PDF_BUSY_MESSAGE = (
    "The PDF reader is busy right now. Please try again in a moment, "
    "or upload a PNG or JPG of the receipt."
)
_PDF_CLAIMED_TYPES = frozenset({"application/pdf", "application/x-pdf", "application/acrobat"})
# PDF readers accept the header anywhere in the first 1024 bytes.
_PDF_HEADER = b"%PDF-"
_PDF_HEADER_WINDOW = 1024

# Image decoding is CPU and memory heavy.  ONE image check runs at a time per
# process (Render free plan: 512 MB), whatever the request concurrency, and the
# decode itself is only attempted when its estimated memory cost (from the
# header, see ``_jpeg_decode_cost`` / ``_WEBP_DECODE_BYTES_PER_PIXEL``) fits
# ``DECODE_BUDGET_BYTES``; above that the image is accepted on its validated
# structure alone.
_DECODE_SLOTS = threading.BoundedSemaphore(1)
DECODE_BUDGET_BYTES = 64 * 1024 * 1024


# ---- Errors ----------------------------------------------------------------


class PayloadTooLargeError(AppError):
    status_code = 413
    code = ErrorCode.PAYLOAD_TOO_LARGE


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE


class InvalidUploadError(AppError):
    status_code = 422
    code = INVALID_UPLOAD


class PdfRendererBusyError(AppError):
    """503: the one PDF render slot stayed busy for the whole queue wait.

    Retryable (``Retry-After``: the render timeout, rounded up) — the file
    itself was never looked at, so it is not refused like a damaged one.
    """

    status_code = 503
    code = "PDF_RENDERER_BUSY"

    def __init__(self) -> None:
        super().__init__(PDF_BUSY_MESSAGE)
        seconds = max(1, math.ceil(get_settings().UPLOAD_PDF_RENDER_TIMEOUT_SECONDS))
        self.headers = {"Retry-After": str(seconds)}


class UploadQuotaExceededError(AppError):
    status_code = 409
    code = UPLOAD_QUOTA_EXCEEDED


@dataclass(frozen=True)
class CheckedUpload:
    """A validated, sanitised upload ready to store."""

    data: bytes
    kind: str
    # Phase 12: "pdf" when ``data`` is the PNG rendered from an uploaded PDF
    # (which itself was discarded), with the PDF's page count and how many
    # pages the PNG shows.
    source: str = "image"
    pages_total: int | None = None
    pages_used: int | None = None

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES[self.kind]

    @property
    def extension(self) -> str:
        return EXTENSIONS[self.kind]


# ---- Reading ---------------------------------------------------------------


async def read_upload_bounded(file: UploadFile, limit: int) -> bytes:
    """Read *file* in chunks; 413 as soon as it is larger than *limit* bytes.

    Holds at most ``limit`` plus one chunk in memory.
    """
    buf = bytearray()
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > limit:
            from backend.app.core.body_limit import upload_too_large_message  # noqa: PLC0415

            raise PayloadTooLargeError(upload_too_large_message(limit))


# ---- File names ------------------------------------------------------------

_UNSAFE_NAME_CHARS = re.compile(r"[\\/:*?\"<>|]")


def safe_filename(name: str | None, *, max_length: int = 120) -> str:
    """A display-safe version of a client file name (never used in a key).

    NFKC-normalised (full-width and compatibility look-alikes folded), path
    components dropped (both ``/`` and ``\\``), control and format characters
    removed (incl. bidi overrides such as U+202E that disguise an extension),
    reserved characters replaced, leading dots stripped, length capped.  Returns
    ``"receipt"`` when nothing usable is left.
    """
    if not name:
        return "receipt"
    text = unicodedata.normalize("NFKC", name)
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = _UNSAFE_NAME_CHARS.sub("_", text).strip().lstrip(".").strip()
    if len(text) > max_length:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            text = stem[: max_length - len(ext) - 1] + "." + ext
        else:
            text = text[:max_length]
    return text or "receipt"


def _extension_of(name: str | None) -> str | None:
    """The lower-case extension of the sanitised name, or None when there is none."""
    clean = safe_filename(name)
    if "." not in clean:
        return None
    return clean.rsplit(".", 1)[-1].lower()


# ---- Sniffing --------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def sniff_kind(data: bytes) -> str | None:
    """The allow-listed kind the bytes ARE (by magic number), or None."""
    if data.startswith(b"\xff\xd8\xff"):
        return KIND_JPEG
    if data.startswith(_PNG_SIG):
        return KIND_PNG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return KIND_WEBP
    return None


def _has_pdf_header(data: bytes) -> bool:
    """True when a PDF reader would take *data* for a PDF (header in the first KiB)."""
    return _PDF_HEADER in data[: _PDF_HEADER_WINDOW + len(_PDF_HEADER)]


def _claims_pdf(*, filename: str | None, content_type: str | None) -> bool:
    """True when the client declares a PDF type or names a ``.pdf`` file."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base in _PDF_CLAIMED_TYPES or _extension_of(filename) == "pdf"


def _claimed_kind(content_type: str | None) -> tuple[bool, str | None]:
    """(known, kind) for the client's Content-Type; parameters are ignored."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base not in _CLAIMED_TYPES:
        return False, None
    return True, _CLAIMED_TYPES[base]


def inspect_upload(data: bytes, *, filename: str | None, content_type: str | None) -> CheckedUpload:
    """Validate and sanitise an upload; raise 413/415/422 ``AppError``s.

    Decides the type from the content, then checks the claimed type and the
    extension agree with it, then validates and sanitises per type.
    """
    settings = get_settings()
    if not data:
        raise InvalidUploadError("The file is empty.")
    if len(data) > settings.MAX_UPLOAD_BYTES:
        from backend.app.core.body_limit import upload_too_large_message  # noqa: PLC0415

        raise PayloadTooLargeError(upload_too_large_message(settings.MAX_UPLOAD_BYTES))

    kind = sniff_kind(data)
    pdf_bytes = _has_pdf_header(data)
    if pdf_bytes and kind is not None:
        # An image to an image decoder AND a PDF to a PDF reader: ambiguous.
        raise UnsupportedMediaTypeError(POLYGLOT_MESSAGE)
    if pdf_bytes or _claims_pdf(filename=filename, content_type=content_type):
        return _inspect_pdf(data, filename=filename, content_type=content_type)

    if kind is None:
        raise UnsupportedMediaTypeError(
            f"This file type isn't supported. {SUPPORTED_TYPES_MESSAGE}"
        )

    known, claimed = _claimed_kind(content_type)
    if not known or (claimed is not None and claimed != kind):
        raise UnsupportedMediaTypeError(
            "The file's declared type doesn't match its contents. " + SUPPORTED_TYPES_MESSAGE
        )
    ext = _extension_of(filename)
    if ext is not None and _EXTENSION_KINDS.get(ext) != kind:
        raise UnsupportedMediaTypeError(
            "The file name's extension doesn't match its contents. " + SUPPORTED_TYPES_MESSAGE
        )

    limits = _ImageLimits(
        max_pixels=settings.UPLOAD_MAX_IMAGE_PIXELS,
        max_dimension=settings.UPLOAD_MAX_IMAGE_DIMENSION,
    )
    with _DECODE_SLOTS:
        if kind == KIND_PNG:
            clean = _sanitize_png(data, limits)
        elif kind == KIND_JPEG:
            clean = _sanitize_jpeg(data, limits)
        else:
            clean = _sanitize_webp(data, limits)
    if _has_pdf_header(clean):
        # Defence in depth: whatever the sanitiser kept must not read as a PDF.
        raise UnsupportedMediaTypeError(POLYGLOT_MESSAGE)
    return CheckedUpload(data=clean, kind=kind)


# ---- PDF (rasterise-only) --------------------------------------------------

_PDF_ERROR_MESSAGES = {
    "encrypted": PDF_ENCRYPTED_MESSAGE,
    "no_pages": PDF_NO_PAGES_MESSAGE,
}


def _page_too_large_message() -> str:
    inches = get_settings().UPLOAD_PDF_MAX_PAGE_INCHES
    return (
        "This PDF's pages are too large to be a receipt (the maximum is "
        f"{inches:g} × {inches:g} inches)."
    )


def _inspect_pdf(data: bytes, *, filename: str | None, content_type: str | None) -> CheckedUpload:
    """Rasterise a PDF upload in a contained child; return the checked PNG.

    The PDF bytes are never returned, stored or logged — only the rendered PNG,
    which then passes the same checks and sanitiser as an uploaded PNG.
    """
    from backend.app.modules.receipt import pdf_raster  # noqa: PLC0415

    if not _has_pdf_header(data):
        raise UnsupportedMediaTypeError(PDF_MISMATCH_MESSAGE)
    known, claimed = _claimed_kind(content_type)
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base not in _PDF_CLAIMED_TYPES and (not known or claimed is not None):
        raise UnsupportedMediaTypeError(
            "The file's declared type doesn't match its contents. " + SUPPORTED_TYPES_MESSAGE
        )
    ext = _extension_of(filename)
    if ext is not None and ext != "pdf":
        raise UnsupportedMediaTypeError(
            "The file name's extension doesn't match its contents. " + SUPPORTED_TYPES_MESSAGE
        )

    try:
        rendered = pdf_raster.rasterize_pdf(data)
    except pdf_raster.RenderBusyError as exc:
        logger.warning("upload: PDF renderer busy, refused after the queue wait")
        raise PdfRendererBusyError() from exc
    except pdf_raster.PdfRenderError as exc:
        logger.info("upload: PDF refused by the renderer (%s)", exc.code)
        if exc.code == "page_too_large":
            raise InvalidUploadError(_page_too_large_message()) from exc
        raise InvalidUploadError(_PDF_ERROR_MESSAGES.get(exc.code, PDF_UNREADABLE_MESSAGE)) from exc
    except pdf_raster.ChildFailedError as exc:
        logger.warning("upload: PDF render failed (%s)", exc.reason)
        raise InvalidUploadError(PDF_UNREADABLE_MESSAGE) from exc
    except OSError as exc:  # the child could not be started at all
        logger.warning("upload: PDF renderer unavailable (%s)", type(exc).__name__)
        raise InvalidUploadError(PDF_UNREADABLE_MESSAGE) from exc

    try:
        checked = inspect_upload(rendered.png, filename=None, content_type="image/png")
    except AppError as exc:
        logger.warning("upload: rendered PDF page failed the image checks (%s)", exc.code)
        raise InvalidUploadError(PDF_UNREADABLE_MESSAGE) from exc
    if checked.kind != KIND_PNG:  # pragma: no cover — the worker only writes PNG
        raise InvalidUploadError(PDF_UNREADABLE_MESSAGE)
    return CheckedUpload(
        data=checked.data,
        kind=checked.kind,
        source=KIND_PDF_SOURCE,
        pages_total=rendered.pages_total,
        pages_used=rendered.pages_used,
    )


# ---- Image limits ----------------------------------------------------------


@dataclass(frozen=True)
class _ImageLimits:
    max_pixels: int
    max_dimension: int

    def check(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise InvalidUploadError("The image has no pixels (zero width or height).")
        if width > self.max_dimension or height > self.max_dimension:
            raise InvalidUploadError(
                f"The image is too large ({width}×{height}); the maximum side is "
                f"{self.max_dimension} pixels."
            )
        if width * height > self.max_pixels:
            raise InvalidUploadError(
                f"The image has too many pixels ({width}×{height}); the maximum is "
                f"{self.max_pixels // 1_000_000} megapixels."
            )


_CORRUPT = "The image is damaged or incomplete and can't be read."


# ---- EXIF orientation ------------------------------------------------------


def _exif_orientation(tiff: bytes) -> int | None:
    """Orientation (tag 0x0112) from a TIFF-structured EXIF block, or None."""
    try:
        if tiff[:2] == b"II":
            endian = "<"
        elif tiff[:2] == b"MM":
            endian = ">"
        else:
            return None
        (ifd_offset,) = struct.unpack(endian + "I", tiff[4:8])
        (count,) = struct.unpack(endian + "H", tiff[ifd_offset : ifd_offset + 2])
        for i in range(min(count, 512)):
            entry = tiff[ifd_offset + 2 + 12 * i : ifd_offset + 14 + 12 * i]
            tag, typ, _n = struct.unpack(endian + "HHI", entry[:8])
            if tag == 0x0112 and typ == 3:
                (value,) = struct.unpack(endian + "H", entry[8:10])
                return value if 1 <= value <= 8 else None
    except (struct.error, IndexError):
        return None
    return None


def _orientation_only_tiff(orientation: int) -> bytes:
    """A minimal big-endian TIFF/EXIF block with ONLY the orientation tag."""
    return (
        b"MM\x00\x2a"
        + struct.pack(">I", 8)
        + struct.pack(">H", 1)
        + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0)
        + struct.pack(">I", 0)
    )


# ---- PNG -------------------------------------------------------------------

# Chunks that are kept: critical chunks plus colour / transparency / physical
# size information that changes how pixels render.  Everything else — tEXt,
# zTXt, iTXt, eXIf, tIME, private and animation chunks — is dropped.
_PNG_KEEP = frozenset(
    {
        b"IHDR",
        b"PLTE",
        b"IDAT",
        b"IEND",
        b"tRNS",
        b"gAMA",
        b"cHRM",
        b"sRGB",
        b"iCCP",
        b"sBIT",
        b"pHYs",
    }
)
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def _png_raw_size(width: int, height: int, bit_depth: int, color_type: int, interlace: int) -> int:
    bpp = _PNG_CHANNELS[color_type] * bit_depth

    def row_bytes(w: int) -> int:
        return (w * bpp + 7) // 8

    if interlace == 0:
        return height * (1 + row_bytes(width))
    total = 0
    for x0, y0, dx, dy in _ADAM7:
        pw = (width - x0 + dx - 1) // dx if width > x0 else 0
        ph = (height - y0 + dy - 1) // dy if height > y0 else 0
        if pw and ph:
            total += ph * (1 + row_bytes(pw))
    return total


def _sanitize_png(data: bytes, limits: _ImageLimits) -> bytes:
    """Validate chunk structure + CRCs, the decompressed size, and strip metadata."""
    pos = len(_PNG_SIG)
    out = bytearray(_PNG_SIG)
    idat = zlib.decompressobj()
    expected = 0
    produced = 0
    seen_ihdr = seen_idat = seen_iend = False
    while pos < len(data):
        if pos + 8 > len(data):
            raise InvalidUploadError(_CORRUPT)
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > len(data):
            raise InvalidUploadError(_CORRUPT)
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : end])
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise InvalidUploadError(_CORRUPT)
        if not seen_ihdr:
            if ctype != b"IHDR" or length != 13:
                raise InvalidUploadError(_CORRUPT)
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            limits.check(width, height)
            if color not in _PNG_CHANNELS or interlace not in (0, 1):
                raise InvalidUploadError(_CORRUPT)
            expected = _png_raw_size(width, height, depth, color, interlace)
            seen_ihdr = True
        elif ctype == b"IDAT":
            seen_idat = True
            # Stream-decompress and DISCARD: proves the pixel data is intact and
            # exactly as large as the header says, in bounded memory.
            try:
                chunk = idat.decompress(body, _READ_CHUNK)
                produced += len(chunk)
                while idat.unconsumed_tail and produced <= expected:
                    chunk = idat.decompress(idat.unconsumed_tail, _READ_CHUNK)
                    produced += len(chunk)
            except zlib.error as exc:
                raise InvalidUploadError(_CORRUPT) from exc
            if produced > expected:
                raise InvalidUploadError(_CORRUPT)
        if ctype in _PNG_KEEP:
            out += data[pos:end]
        pos = end
        if ctype == b"IEND":
            seen_iend = True
            break  # anything after IEND (a polyglot payload) is dropped
    if not (seen_ihdr and seen_idat and seen_iend):
        raise InvalidUploadError(_CORRUPT)
    try:
        produced += len(idat.flush())
    except zlib.error as exc:
        raise InvalidUploadError(_CORRUPT) from exc
    if not idat.eof or produced != expected:
        raise InvalidUploadError(_CORRUPT)
    return bytes(out)


# ---- JPEG ------------------------------------------------------------------

_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
# Segments kept: tables, frame/scan headers, restart interval, JFIF (APP0),
# ICC profile (APP2 "ICC_PROFILE") and Adobe colour transform (APP14).  APP1
# (EXIF / XMP), other APPn and COM are dropped.
_JPEG_KEEP = frozenset({0xC4, 0xCC, 0xDB, 0xDD, 0xE0, 0xEE}) | _SOF_MARKERS


def _sanitize_jpeg(data: bytes, limits: _ImageLimits) -> bytes:
    out = bytearray(b"\xff\xd8")
    pos = 2
    orientation: int | None = None
    exif_insert_at: int | None = None
    size: tuple[int, int] | None = None
    sof: tuple[int, bytes] | None = None
    # Ns of every scan, in order (integration review fix: the decode-cost
    # estimate must see what libjpeg will see).
    scan_components: list[int] = []
    n = len(data)
    while True:
        if pos >= n or data[pos] != 0xFF:
            raise InvalidUploadError(_CORRUPT)
        while pos < n and data[pos] == 0xFF:
            pos += 1  # fill bytes
        if pos >= n:
            raise InvalidUploadError(_CORRUPT)
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI — anything after it is dropped
            out += b"\xff\xd9"
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += bytes((0xFF, marker))
            continue
        if pos + 2 > n:
            raise InvalidUploadError(_CORRUPT)
        (seg_len,) = struct.unpack(">H", data[pos : pos + 2])
        if seg_len < 2 or pos + seg_len > n:
            raise InvalidUploadError(_CORRUPT)
        segment = data[pos + 2 : pos + seg_len]
        raw = bytes((0xFF, marker)) + data[pos : pos + seg_len]
        pos += seg_len
        if marker in _SOF_MARKERS:
            if sof is not None:
                # One frame per image.  A second SOF (a hierarchical file, or a
                # parser-differential trick) is refused: our estimate and the
                # decoder could otherwise read different frames.
                raise InvalidUploadError(_CORRUPT)
            if len(segment) < 6:
                raise InvalidUploadError(_CORRUPT)
            height, width = struct.unpack(">HH", segment[1:5])
            limits.check(width, height)
            size = (width, height)
            sof = (marker, segment)
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00") and orientation is None:
            orientation = _exif_orientation(segment[6:])
        keep = marker in _JPEG_KEEP or (marker == 0xE2 and segment.startswith(b"ICC_PROFILE\x00"))
        if marker == 0xDA:  # SOS: copy header + entropy-coded data up to the next marker
            if size is None or sof is None:
                raise InvalidUploadError(_CORRUPT)
            scan_components.append(_scan_component_count(segment, sof[1]))
            out += raw
            start = pos
            while True:
                nxt = data.find(b"\xff", pos)
                if nxt < 0 or nxt + 1 >= n:
                    raise InvalidUploadError(_CORRUPT)  # truncated scan
                follower = data[nxt + 1]
                if follower == 0x00 or 0xD0 <= follower <= 0xD7 or follower == 0xFF:
                    pos = nxt + 1 if follower == 0xFF else nxt + 2
                    continue
                break
            if nxt == start:
                raise InvalidUploadError(_CORRUPT)  # a scan with no image data
            out += data[start:nxt]
            pos = nxt
            continue
        if keep:
            out += raw
            if marker == 0xE0 and exif_insert_at is None:
                exif_insert_at = len(out)
    if not scan_components or size is None or sof is None:
        raise InvalidUploadError(_CORRUPT)
    if orientation is not None and orientation != 1:
        tiff = b"Exif\x00\x00" + _orientation_only_tiff(orientation)
        app1 = b"\xff\xe1" + struct.pack(">H", len(tiff) + 2) + tiff
        at = exif_insert_at if exif_insert_at is not None else 2
        out[at:at] = app1
    clean = bytes(out)
    if _jpeg_decode_cost(sof[0], sof[1], scan_components) <= DECODE_BUDGET_BYTES:
        _decode_check(clean, size, reduce=True)
    else:
        logger.info("upload: JPEG decode check skipped (over the memory budget)")
    return clean


# The only frames decoded MCU row by MCU row: baseline / extended sequential
# Huffman.  Every other SOF (progressive, lossless, arithmetic, hierarchical /
# differential) is costed at the worst case.
_STREAMING_SOF = frozenset({0xC0, 0xC1})


def _sof_component_ids(sof: bytes) -> list[int]:
    ncomp = sof[5] if len(sof) > 5 else 0
    comps = sof[6 : 6 + 3 * ncomp]
    if ncomp == 0 or len(comps) != 3 * ncomp:
        raise InvalidUploadError(_CORRUPT)
    return [comps[3 * i] for i in range(ncomp)]


def _scan_component_count(sos: bytes, sof: bytes) -> int:
    """Ns of a scan header, validated against the frame (422 when malformed).

    SOS = Ns, Ns x (component id, table selectors), Ss, Se, Ah/Al: exactly
    1 + 2*Ns + 3 bytes; 1 <= Ns <= 4; every id one of the frame's, none twice.
    """
    if not sos:
        raise InvalidUploadError(_CORRUPT)
    ns = sos[0]
    frame_ids = _sof_component_ids(sof)
    if not 1 <= ns <= min(4, len(frame_ids)) or len(sos) != 1 + 2 * ns + 3:
        raise InvalidUploadError(_CORRUPT)
    ids = [sos[1 + 2 * i] for i in range(ns)]
    if len(set(ids)) != ns or any(cid not in frame_ids for cid in ids):
        raise InvalidUploadError(_CORRUPT)
    return ns


def _jpeg_decode_cost(marker: int, sof: bytes, scan_components: Sequence[int]) -> int:
    """Estimated bytes libjpeg allocates to decode this frame at 1/8 scale.

    A sequential (SOF0 / SOF1) JPEG with exactly ONE scan carrying EVERY frame
    component is decoded MCU row by MCU row, so at 1/8 scale it costs about one
    byte per 8x8 block per component.  Anything else — a progressive, lossless,
    arithmetic or hierarchical frame, several scans, or a first scan with fewer
    components than the frame (Ns < Nf, which already makes libjpeg allocate
    the full coefficient buffer) — is costed at the WORST case: every DCT
    coefficient (64 x 2 bytes per block, per component, padded to whole MCUs);
    ``draft`` does not help.  Unreadable component data is costed as
    unaffordable (the structure walk already capped the dimensions).
    """
    height, width = struct.unpack(">HH", sof[1:5])
    ncomp = sof[5] if len(sof) > 5 else 0
    comps = sof[6 : 6 + 3 * ncomp]
    if ncomp == 0 or len(comps) != 3 * ncomp:
        return DECODE_BUDGET_BYTES + 1
    factors = [(comps[3 * i + 1] >> 4, comps[3 * i + 1] & 0x0F) for i in range(ncomp)]
    if any(not (1 <= h <= 4 and 1 <= v <= 4) for h, v in factors):
        return DECODE_BUDGET_BYTES + 1
    h_max = max(h for h, _ in factors)
    v_max = max(v for _, v in factors)
    mcus_wide = -(-width // (8 * h_max))
    mcus_high = -(-height // (8 * v_max))
    blocks = sum(mcus_wide * h * mcus_high * v for h, v in factors)
    streaming = (
        marker in _STREAMING_SOF and len(scan_components) == 1 and scan_components[0] == ncomp
    )
    return blocks if streaming else blocks * 64 * 2


# ---- WebP ------------------------------------------------------------------


def _sanitize_webp(data: bytes, limits: _ImageLimits) -> bytes:
    """Validate the RIFF container, check dimensions, drop EXIF/XMP chunks."""
    if len(data) < 20:
        raise InvalidUploadError(_CORRUPT)
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size + 8 > len(data) or riff_size < 4:
        raise InvalidUploadError(_CORRUPT)
    data = data[: riff_size + 8]  # bytes past the RIFF end are dropped
    chunks: list[tuple[bytes, bytes]] = []
    pos = 12
    while pos < len(data):
        if pos + 8 > len(data):
            raise InvalidUploadError(_CORRUPT)
        fourcc = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body_end = pos + 8 + size
        if body_end > len(data):
            raise InvalidUploadError(_CORRUPT)
        chunks.append((fourcc, data[pos + 8 : body_end]))
        pos = body_end + (size & 1)
    if not chunks:
        raise InvalidUploadError(_CORRUPT)

    size_wh = _webp_size(chunks)
    limits.check(*size_wh)

    orientation: int | None = None
    kept: list[tuple[bytes, bytes]] = []
    for fourcc, body in chunks:
        if fourcc == b"EXIF":
            tiff = body[6:] if body.startswith(b"Exif\x00\x00") else body
            orientation = _exif_orientation(tiff)
            continue
        if fourcc == b"XMP ":
            continue
        kept.append((fourcc, body))
    if kept[0][0] == b"VP8X":
        flags = kept[0][1][0] & ~0x0C  # clear the EXIF (0x08) and XMP (0x04) bits
        if orientation is not None and orientation != 1:
            flags |= 0x08
            kept.append((b"EXIF", _orientation_only_tiff(orientation)))
        kept[0] = (b"VP8X", bytes((flags,)) + kept[0][1][1:])
    body = bytearray(b"WEBP")
    for fourcc, chunk in kept:
        body += fourcc + struct.pack("<I", len(chunk)) + chunk
        if len(chunk) & 1:
            body += b"\x00"
    clean = b"RIFF" + struct.pack("<I", len(body)) + bytes(body)
    # WebP cannot be decoded at reduced scale, so the decode check runs only
    # when the full decode fits the budget (about 4 MP); a larger WebP is
    # accepted on its validated structure alone (the OCR provider rejects
    # undecodable data).
    if size_wh[0] * size_wh[1] * _WEBP_DECODE_BYTES_PER_PIXEL <= DECODE_BUDGET_BYTES:
        _decode_check(clean, size_wh, reduce=False)
    else:
        logger.info("upload: WebP decode check skipped (over the memory budget)")
    return clean


# Measured peak RSS of Pillow 12's WebP load (RGBA canvas, decoder frame and
# the image copy): ~15.4 bytes per pixel (4 MP -> 62 MB, 16 MP -> 245 MB).
_WEBP_DECODE_BYTES_PER_PIXEL = 16


def _webp_size(chunks: list[tuple[bytes, bytes]]) -> tuple[int, int]:
    fourcc, body = chunks[0]
    try:
        if fourcc == b"VP8X" and len(body) >= 10:
            w = int.from_bytes(body[4:7], "little") + 1
            h = int.from_bytes(body[7:10], "little") + 1
            return w, h
        if fourcc == b"VP8 " and len(body) >= 10 and body[3:6] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", body[6:10])
            return w & 0x3FFF, h & 0x3FFF
        if fourcc == b"VP8L" and len(body) >= 5 and body[0] == 0x2F:
            bits = int.from_bytes(body[1:5], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (struct.error, IndexError):
        pass
    raise InvalidUploadError(_CORRUPT)


# ---- Decode check ----------------------------------------------------------


def _decode_check(data: bytes, size: tuple[int, int], *, reduce: bool) -> None:
    """Decode the sanitised image once to prove it is readable.

    Dimensions were already capped from the header, and the caller has checked
    the decode's estimated memory cost against ``DECODE_BUDGET_BYTES``.  A JPEG
    is decoded at 1/8 scale (``draft``).
    """
    from PIL import Image  # noqa: PLC0415

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as img:
                if img.size != size:
                    raise InvalidUploadError(_CORRUPT)
                if reduce:
                    img.draft("RGB", (max(1, size[0] // 8), max(1, size[1] // 8)))
                img.load()
    except InvalidUploadError:
        raise
    except Exception as exc:  # noqa: BLE001 — any decoder failure is a bad file
        logger.info("upload: image failed the decode check (%s)", type(exc).__name__)
        raise InvalidUploadError(_CORRUPT) from exc
