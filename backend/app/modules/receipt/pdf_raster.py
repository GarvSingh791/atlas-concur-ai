"""PDF receipts, rasterise-only (phase 12 follow-up).

A PDF receipt is NEVER stored and NEVER served.  It is parsed and rendered by
PDFium (pypdfium2) in a short-lived CHILD process (``pdf_render_worker.py``)
into one PNG — the first ``UPLOAD_PDF_MAX_PAGES`` pages stacked vertically —
and only that PNG continues through the normal image pipeline (magic-byte
sniff, decompression-bomb limits, sanitiser, quota, storage, OCR).  Scripts,
launch actions, embedded files, forms and XFA cannot survive rendering, so
nothing has to be "inspected" — the attack surface left is PDFium's parser and
rasteriser, and it runs contained:

* a separate process (``python -I -B``, empty environment, its own session /
  process group, a temporary working directory removed afterwards), never the
  request worker;
* a wall-clock timeout enforced here (the whole process group is SIGKILLed —
  no orphan survives), a CPU-seconds cap, an address-space growth cap, no file
  writes (``RLIMIT_FSIZE`` 0) and no new descriptors (``RLIMIT_NOFILE`` 0, so
  no socket or file can be opened) set inside the child (see the worker's docstring);
* the answer is read over a pipe with a hard size bound; anything unexpected —
  a timeout, a crash, a signal, a non-zero exit, a malformed or oversized
  answer — is a 422 "could not read this PDF", never a 500.

ONE render runs at a time per process (``_RENDER_SLOTS``), and a request waits
for that slot at most ``UPLOAD_PDF_QUEUE_WAIT_SECONDS``: past that it is
refused as BUSY (``RenderBusyError`` -> a retryable 503 in ``upload_guard``),
so slow or hostile PDFs can never build an unbounded queue of waiting requests
(each would pin a threadpool thread and its request's DB connection).
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config.settings import get_settings

logger = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).with_name("pdf_render_worker.py")
MAGIC = b"ATPDF1"
# Points per inch (PDF user space).
POINTS_PER_INCH = 72.0
# Never upscale a small page beyond 200 DPI.
MAX_SCALE = 200.0 / POINTS_PER_INCH
# Slack for the frame header around the PNG on the pipe.
_HEADER_SLACK = 4096
_READ_CHUNK = 64 * 1024

_RENDER_SLOTS = threading.BoundedSemaphore(1)


class ChildFailedError(Exception):
    """The child timed out, crashed, overflowed the pipe or answered garbage."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RenderBusyError(Exception):
    """The render slot stayed busy for the whole queue wait; nothing was rendered."""


class PdfRenderError(Exception):
    """The worker answered with an error code (``encrypted``, ``no_pages``, ...)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RenderLimits:
    timeout_seconds: float
    memory_bytes: int
    cpu_seconds: int
    max_pages: int
    max_page_points: float
    max_side_px: int
    max_input_bytes: int
    max_output_bytes: int
    queue_wait_seconds: float

    @classmethod
    def from_settings(cls) -> RenderLimits:
        s = get_settings()
        return cls(
            timeout_seconds=float(s.UPLOAD_PDF_RENDER_TIMEOUT_SECONDS),
            memory_bytes=int(s.UPLOAD_PDF_RENDER_MEMORY_BYTES),
            cpu_seconds=int(s.UPLOAD_PDF_RENDER_CPU_SECONDS),
            max_pages=int(s.UPLOAD_PDF_MAX_PAGES),
            max_page_points=float(s.UPLOAD_PDF_MAX_PAGE_INCHES) * POINTS_PER_INCH,
            max_side_px=int(s.UPLOAD_PDF_MAX_SIDE_PX),
            max_input_bytes=int(s.MAX_UPLOAD_BYTES),
            max_output_bytes=int(s.MAX_UPLOAD_BYTES),
            queue_wait_seconds=float(s.UPLOAD_PDF_QUEUE_WAIT_SECONDS),
        )


@dataclass(frozen=True)
class RenderedPdf:
    png: bytes
    pages_total: int
    pages_used: int


def worker_command(limits: RenderLimits) -> list[str]:
    opts = {
        "cpu_seconds": limits.cpu_seconds,
        "memory_bytes": limits.memory_bytes,
        "max_pages": limits.max_pages,
        "max_page_points": limits.max_page_points,
        "max_side_px": limits.max_side_px,
        "max_scale": MAX_SCALE,
        "max_input_bytes": limits.max_input_bytes,
        "max_output_bytes": limits.max_output_bytes,
    }
    return [sys.executable, "-I", "-B", str(WORKER_PATH), json.dumps(opts)]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group (it is the session leader) and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover — SIGKILL cannot be ignored
        logger.warning("pdf render: child did not exit after SIGKILL")


def run_child(cmd: list[str], payload: bytes, *, timeout: float, max_output: int) -> bytes:
    """Run *cmd* with *payload* on stdin; return its stdout.

    Raises ``ChildFailedError`` when the wall clock exceeds *timeout*, stdout
    grows beyond *max_output* bytes, or the process exits non-zero / by a
    signal.  The process group is always killed and reaped, and the temporary
    working directory removed, before this returns.
    """
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="atlas-pdf-") as tmp:
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "TMPDIR": tmp, "LC_ALL": "C.UTF-8"}
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tmp,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        try:
            out = _exchange(proc, payload, deadline=deadline, max_output=max_output)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChildFailedError("timeout")
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise ChildFailedError("timeout") from exc
            if code != 0:
                raise ChildFailedError(f"exit {code}")
            return out
        finally:
            _kill_group(proc)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def _exchange(
    proc: subprocess.Popen[bytes], payload: bytes, *, deadline: float, max_output: int
) -> bytes:
    """Write *payload* to stdin and read stdout to EOF, both non-blocking and bounded."""
    assert proc.stdin is not None and proc.stdout is not None
    stdin_fd, stdout_fd = proc.stdin.fileno(), proc.stdout.fileno()
    os.set_blocking(stdin_fd, False)
    os.set_blocking(stdout_fd, False)
    view = memoryview(payload)
    sent = 0
    out = bytearray()
    with selectors.DefaultSelector() as sel:
        sel.register(stdout_fd, selectors.EVENT_READ)
        if view:
            sel.register(stdin_fd, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChildFailedError("timeout")
            for key, _events in sel.select(timeout=remaining):
                if key.fd == stdin_fd:
                    try:
                        sent += os.write(stdin_fd, view[sent : sent + _READ_CHUNK])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        sent = len(view)  # the child stopped reading; its answer decides
                    if sent >= len(view):
                        sel.unregister(stdin_fd)
                        proc.stdin.close()
                else:
                    try:
                        chunk = os.read(stdout_fd, _READ_CHUNK)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        return bytes(out)
                    out += chunk
                    if len(out) > max_output:
                        raise ChildFailedError("output too large")


def parse_answer(raw: bytes) -> tuple[dict[str, object], bytes]:
    """(header, body) of the worker's framed answer; ``ChildFailedError`` when malformed."""
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 4:
        raise ChildFailedError("malformed answer")
    (hlen,) = struct.unpack(">I", raw[len(MAGIC) : len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if hlen > _HEADER_SLACK or start + hlen > len(raw):
        raise ChildFailedError("malformed answer")
    try:
        header = json.loads(raw[start : start + hlen])
    except ValueError as exc:
        raise ChildFailedError("malformed answer") from exc
    if not isinstance(header, dict):
        raise ChildFailedError("malformed answer")
    return header, raw[start + hlen :]


def rasterize_pdf(data: bytes, limits: RenderLimits | None = None) -> RenderedPdf:
    """Render the first pages of the PDF *data* into ONE PNG in a contained child.

    Raises ``RenderBusyError`` when the render slot is not free within
    ``limits.queue_wait_seconds`` (the wait is bounded: no request queues
    behind a slow render indefinitely), ``PdfRenderError`` (the worker's error
    code) or ``ChildFailedError``.
    """
    limits = limits or RenderLimits.from_settings()
    if not _RENDER_SLOTS.acquire(timeout=limits.queue_wait_seconds):
        raise RenderBusyError()
    try:
        started = time.monotonic()
        raw = run_child(
            worker_command(limits),
            data,
            timeout=limits.timeout_seconds,
            max_output=limits.max_output_bytes + _HEADER_SLACK,
        )
    finally:
        _RENDER_SLOTS.release()
    header, png = parse_answer(raw)
    error = header.get("error")
    if error is not None:
        raise PdfRenderError(str(error))
    total, used = header.get("pages_total"), header.get("pages_used")
    if (
        not isinstance(total, int)
        or not isinstance(used, int)
        or not 1 <= used <= min(total, limits.max_pages)
        or not png
    ):
        raise ChildFailedError("malformed answer")
    logger.info(
        "pdf render: %d of %d page(s) in %.2fs, %d PNG bytes",
        used,
        total,
        time.monotonic() - started,
        len(png),
    )
    return RenderedPdf(png=png, pages_total=total, pages_used=used)
