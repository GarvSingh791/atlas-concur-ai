"""Phase 12 follow-up: PDF receipts, rasterise-only — hermetic.

A PDF is never stored or served: it is rendered by PDFium in a contained child
process (``pdf_raster`` / ``pdf_render_worker``) into ONE PNG, and only that
PNG goes on through the image pipeline.  Covered here:

* the guard end to end with the REAL child: 1- / 3- / 500-page PDFs, active
  content (JavaScript, OpenAction, Launch, embedded file, XFA) that cannot
  survive, encrypted / truncated / corrupt / page-less / huge-MediaBox PDFs
  (422), an image-stream bomb (bounded), trailing garbage, lying types;
* the worker's own rules in-process (sizes checked BEFORE any page renders,
  page cap, pixel cap, output-size shrink);
* the child-process containment with stub programs (memory cap, CPU cap, no
  file writes, no new descriptors / sockets, an empty environment, wall-clock
  timeout that kills the whole process group, bounded output, crashes, the
  temporary directory removed);
* the framing / validation of the child's answer with fake renderers, and the
  guard's mapping of every failure to a 422 (never a 500).
"""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from backend.app.modules.receipt import pdf_raster as pr
from backend.app.modules.receipt import pdf_render_worker as worker
from backend.app.modules.receipt import upload_guard as g
from backend.tests import pdf_fixtures as f
from config.settings import get_settings

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _inspect(data: bytes, name: str | None = "receipt.pdf", ctype: str | None = None) -> Any:
    return g.inspect_upload(data, filename=name, content_type=ctype)


def _size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as img:
        img.load()
        return img.size


def _limits(**kw: Any) -> pr.RenderLimits:
    base: dict[str, Any] = {
        "timeout_seconds": 10.0,
        "memory_bytes": 256 * 2**20,
        "cpu_seconds": 10,
        "max_pages": 3,
        "max_page_points": 20 * 72.0,
        "max_side_px": 2000,
        "max_input_bytes": 10 * 2**20,
        "max_output_bytes": 10 * 2**20,
        "queue_wait_seconds": 3.0,
    }
    base.update(kw)
    return pr.RenderLimits(**base)


# ---------------------------------------------------------------------------
# 1. Settings
# ---------------------------------------------------------------------------


def test_pdf_render_defaults() -> None:
    s = get_settings()
    assert s.UPLOAD_PDF_MAX_PAGES == 3
    assert s.UPLOAD_PDF_MAX_PAGE_INCHES == 20.0
    assert s.UPLOAD_PDF_MAX_SIDE_PX == 2000
    assert s.UPLOAD_PDF_RENDER_TIMEOUT_SECONDS == 10.0
    assert s.UPLOAD_PDF_RENDER_CPU_SECONDS == 10
    assert s.UPLOAD_PDF_RENDER_MEMORY_BYTES == 256 * 2**20
    assert s.UPLOAD_PDF_QUEUE_WAIT_SECONDS == 3.0
    limits = pr.RenderLimits.from_settings()
    assert limits.queue_wait_seconds == 3.0
    assert limits.max_input_bytes == s.MAX_UPLOAD_BYTES
    assert limits.max_page_points == 1440.0


# ---------------------------------------------------------------------------
# 2. The guard with the real child: only a PNG comes out
# ---------------------------------------------------------------------------


def test_one_page_pdf_becomes_one_png() -> None:
    checked = _inspect(f.text_pdf(1), "receipt.pdf", "application/pdf")
    assert checked.source == "pdf"
    assert checked.kind == "png" and checked.content_type == "image/png"
    assert (checked.pages_total, checked.pages_used) == (1, 1)
    assert checked.data.startswith(PNG_SIG)
    assert b"%PDF" not in checked.data
    w, h = _size(checked.data)
    assert max(w, h) == 2000  # a Letter page scaled to the 2000 px cap
    with Image.open(io.BytesIO(checked.data)) as img:
        grey = img.convert("L")
        assert grey.getextrema() == (0, 255)  # the text was drawn, not a blank page


def test_three_page_pdf_is_stitched_vertically() -> None:
    checked = _inspect(f.text_pdf(3))
    assert (checked.pages_total, checked.pages_used) == (3, 3)
    w, h = _size(checked.data)
    one_w, one_h = _size(_inspect(f.text_pdf(1)).data)
    assert w == one_w
    assert h == 3 * one_h + 2 * worker._PAGE_GAP


def test_pillow_written_pdf_is_accepted() -> None:
    checked = _inspect(f.pillow_pdf(2), None, None)
    assert (checked.pages_total, checked.pages_used) == (2, 2)
    assert b"%PDF" not in checked.data


def test_five_hundred_page_pdf_renders_only_the_first_pages_quickly() -> None:
    started = time.monotonic()
    checked = _inspect(f.text_pdf(500))
    assert time.monotonic() - started < 8
    assert (checked.pages_total, checked.pages_used) == (500, 3)
    _w, h = _size(checked.data)
    assert h <= 3 * 2000 + 2 * worker._PAGE_GAP


@pytest.mark.parametrize(
    ("make", "marker"),
    [
        (f.js_openaction_pdf, f.MARK_JS),
        (f.launch_pdf, f.MARK_LAUNCH),
        (f.embedded_file_pdf, f.MARK_EMBEDDED),
        (f.xfa_pdf, f.MARK_XFA),
    ],
    ids=["javascript-openaction", "launch", "embedded-file", "xfa"],
)
def test_active_content_does_not_survive_rasterisation(
    make: Any, marker: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    data = make()
    assert marker in data
    caplog.set_level(logging.DEBUG)
    checked = _inspect(data, "evil.pdf", "application/pdf")
    assert checked.source == "pdf" and checked.kind == "png"
    assert marker not in checked.data
    assert b"%PDF" not in checked.data
    assert b"/JavaScript" not in checked.data and b"/Launch" not in checked.data
    with Image.open(io.BytesIO(checked.data)) as img:
        assert img.format == "PNG"
        assert set(img.info) <= {"dpi", "gamma", "srgb"}  # no text chunks
    logged = caplog.text
    assert marker.decode() not in logged and "evil.pdf" not in logged


def test_pdf_with_trailing_garbage_still_yields_only_a_png() -> None:
    checked = _inspect(f.text_pdf(1) + b"\x00junk" * 500 + b"%PDF-1.7 again")
    assert checked.kind == "png" and b"%PDF" not in checked.data


def test_pdf_with_a_png_appended_is_rendered_not_stored_as_is() -> None:
    png = Image.new("RGB", (8, 8), (1, 2, 3))
    buf = io.BytesIO()
    png.save(buf, "PNG")
    checked = _inspect(f.text_pdf(1) + buf.getvalue())
    assert checked.source == "pdf" and _size(checked.data)[0] > 8


def test_encrypted_pdf_is_422_with_friendly_copy() -> None:
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(f.encrypted_pdf())
    assert exc.value.status_code == 422
    assert exc.value.code == "INVALID_UPLOAD"
    assert exc.value.message == g.PDF_ENCRYPTED_MESSAGE


@pytest.mark.parametrize(
    "data",
    [
        f.text_pdf(1)[: len(f.text_pdf(1)) // 2],
        f.text_pdf(1)[:60],
        b"%PDF-1.4\n" + bytes(range(256)) * 20,
        b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj trailer<</Root 1 0 R>>",
        b"%PDF-",
    ],
    ids=["truncated-half", "truncated-header", "garbage", "no-page-tree", "header-only"],
)
def test_corrupt_or_truncated_pdf_is_422_never_500(data: bytes) -> None:
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(data)
    assert exc.value.status_code == 422
    assert exc.value.message in {g.PDF_UNREADABLE_MESSAGE, g.PDF_NO_PAGES_MESSAGE}


def test_zero_sized_page_is_422() -> None:
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(f.text_pdf(mediabox=(0, 0, 0, 0)))
    assert exc.value.message == g.PDF_NO_PAGES_MESSAGE


@pytest.mark.parametrize(
    "box", [(0, 0, 14400, 14400), (0, 0, 612, 1500), (-800, -800, 800, 800)], ids=str
)
def test_absurd_page_size_is_422(box: tuple[int, int, int, int]) -> None:
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(f.text_pdf(mediabox=box))
    assert exc.value.status_code == 422
    assert "too large to be a receipt" in exc.value.message
    assert "20 × 20 inches" in exc.value.message


def test_image_stream_bomb_is_bounded() -> None:
    """A 200 pt page drawing a 20 000 x 20 000 image (400 MB inflated, ~400 KB
    on the wire) with a TINY memory budget: whatever PDFium does, the result is
    a bounded PNG or a 422 — within the wall clock, never the request worker."""
    data = f.image_bomb_pdf()
    assert len(data) < 2 * 2**20
    started = time.monotonic()
    try:
        out = pr.rasterize_pdf(data, _limits(memory_bytes=16 * 2**20, timeout_seconds=5))
    except (pr.PdfRenderError, pr.ChildFailedError):
        pass
    else:
        assert max(_size(out.png)) <= 2000
    assert time.monotonic() - started < 7


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd.pdf",
        "C:\\\\Windows\\\\réçu.pdf",
        "invoice\u202efdp.png",
        "ｒｅｃｅｉｐｔ．ｐｄｆ",
    ],
    ids=["traversal", "windows-unicode", "bidi-override", "fullwidth"],
)
def test_odd_file_names_are_judged_by_their_sanitised_extension(name: str) -> None:
    safe = g.safe_filename(name)
    assert "/" not in safe and "\\" not in safe and "\u202e" not in safe
    if safe.lower().endswith(".pdf"):
        assert _inspect(f.text_pdf(1), name, None).source == "pdf"
    else:
        with pytest.raises(g.UnsupportedMediaTypeError):
            _inspect(f.text_pdf(1), name, None)


def test_pdf_bytes_over_the_upload_cap_are_413_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an oversized PDF must not be rendered")

    monkeypatch.setattr(pr, "rasterize_pdf", boom)
    data = f.text_pdf(1) + b"\x00" * get_settings().MAX_UPLOAD_BYTES
    with pytest.raises(g.PayloadTooLargeError):
        _inspect(data)


def test_one_render_at_a_time() -> None:
    assert pr._RENDER_SLOTS.acquire(blocking=False)
    try:
        assert not pr._RENDER_SLOTS.acquire(blocking=False), "a second render must wait"
    finally:
        pr._RENDER_SLOTS.release()


# ---- The render slot's wait is bounded (review fix) -------------------------


def _never_launch(_limits: pr.RenderLimits) -> list[str]:
    raise AssertionError("a request that never got the render slot must not start a child")


def test_a_busy_render_slot_fails_fast_instead_of_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render that cannot get the slot within the wait is refused as BUSY.

    Without the bounded acquire this call would wait for as long as the slot is
    held (here: forever), each waiter pinning a threadpool thread and the
    request's DB connection.  It runs in a thread so a regression fails the
    test instead of hanging the suite.
    """
    monkeypatch.setattr(pr, "worker_command", _never_launch)
    outcome: list[object] = []

    def call() -> None:
        try:
            outcome.append(pr.rasterize_pdf(b"%PDF-1.7", _limits(queue_wait_seconds=0.3)))
        except Exception as exc:
            outcome.append(exc)

    assert pr._RENDER_SLOTS.acquire(blocking=False)
    try:
        started = time.monotonic()
        worker_thread = threading.Thread(target=call, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=5)
        elapsed = time.monotonic() - started
        assert not worker_thread.is_alive(), "rasterize_pdf waited for the slot with no limit"
    finally:
        pr._RENDER_SLOTS.release()
        worker_thread.join(timeout=5)
    assert len(outcome) == 1 and isinstance(outcome[0], pr.RenderBusyError)
    assert 0.25 <= elapsed < 3, elapsed
    # The slot was never taken by the refused call: it is free again now.
    assert pr._RENDER_SLOTS.acquire(blocking=False)
    pr._RENDER_SLOTS.release()


def test_a_waiter_gets_the_slot_when_it_frees_within_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_worker(tmp_path, monkeypatch, _frame({"pages_total": 1, "pages_used": 1}, _a_png()))
    assert pr._RENDER_SLOTS.acquire(blocking=False)
    releaser = threading.Timer(0.2, pr._RENDER_SLOTS.release)
    releaser.start()
    try:
        out = pr.rasterize_pdf(b"%PDF-1.7", _limits(queue_wait_seconds=5.0))
    finally:
        releaser.join()
    assert out.pages_used == 1


@pytest.mark.parametrize("failure", ["timeout", "no_child"])
def test_the_slot_is_released_after_a_failed_render(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def timed_out(*_a: Any, **_k: Any) -> bytes:
        raise pr.ChildFailedError("timeout")

    def no_child(_limits: pr.RenderLimits) -> list[str]:
        raise OSError("cannot start")

    if failure == "timeout":
        monkeypatch.setattr(pr, "run_child", timed_out)
    else:
        monkeypatch.setattr(pr, "worker_command", no_child)
    with pytest.raises((pr.ChildFailedError, OSError)):
        pr.rasterize_pdf(b"%PDF-1.7", _limits(queue_wait_seconds=0.1))
    assert pr._RENDER_SLOTS.acquire(blocking=False), "a failed render leaked the slot"
    pr._RENDER_SLOTS.release()


def test_a_busy_renderer_is_a_retryable_503_not_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*_a: Any, **_k: Any) -> Any:
        raise pr.RenderBusyError()

    monkeypatch.setattr(pr, "rasterize_pdf", busy)
    with pytest.raises(g.PdfRendererBusyError) as caught:
        _inspect(f.text_pdf(1))
    exc = caught.value
    assert exc.status_code == 503
    assert exc.code == "PDF_RENDERER_BUSY"
    assert exc.message == g.PDF_BUSY_MESSAGE
    retry_after = int(dict(exc.headers or {})["Retry-After"])
    assert retry_after == 10  # the render timeout, rounded up


# ---------------------------------------------------------------------------
# 3. The worker's rules, in-process
# ---------------------------------------------------------------------------


def _opts(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "max_pages": 3,
        "max_page_points": 1440.0,
        "max_side_px": 2000,
        "max_scale": pr.MAX_SCALE,
        "max_output_bytes": 10 * 2**20,
    }
    base.update(kw)
    return base


def test_worker_checks_every_page_size_before_rendering_any(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdfium2 as pdfium

    rendered: list[int] = []
    real = pdfium.PdfPage.render

    def spy(self: Any, *a: Any, **k: Any) -> Any:
        rendered.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(pdfium.PdfPage, "render", spy)
    # Page 1 is fine; page 3 (the same object, 3 kids) — make ONLY a later page huge.
    data = f.text_pdf(
        3,
        extra_objs={
            102: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 5000 5000] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        },
    )
    header, png = worker.render(data, _opts())
    assert header == {"error": worker.ERR_PAGE_TOO_LARGE} and png == b""
    assert rendered == [], "no page may be rendered before all sizes are checked"


def test_worker_page_beyond_the_cap_is_not_size_checked_or_rendered() -> None:
    data = f.text_pdf(
        4,
        extra_objs={
            103: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 5000 5000] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        },
    )
    header, _png = worker.render(data, _opts())
    assert header == {"pages_total": 4, "pages_used": 3}


def test_worker_small_pages_are_not_upscaled_beyond_200_dpi() -> None:
    header, png = worker.render(f.text_pdf(1, mediabox=(0, 0, 144, 216)), _opts())
    assert header["pages_used"] == 1
    w, h = _size(png)
    assert (w, h) == (400, 600)  # 2 x 3 inches at 200 DPI, well under 2000 px


def test_worker_shrinks_until_the_png_fits_the_output_cap() -> None:
    big = f.text_pdf(1)
    _h, full = worker.render(big, _opts())
    header, png = worker.render(big, _opts(max_output_bytes=len(full) // 2))
    assert "error" not in header
    assert len(png) <= len(full) // 2
    assert max(_size(png)) < 2000
    header, png = worker.render(big, _opts(max_output_bytes=10))
    assert header == {"error": worker.ERR_TOO_LARGE_OUTPUT} and png == b""


# ---------------------------------------------------------------------------
# 4. Containment: stub programs in the real child-process harness
# ---------------------------------------------------------------------------

_LOAD_WORKER = f"""
import importlib.util, os, sys
_spec = importlib.util.spec_from_file_location("w", {str(pr.WORKER_PATH)!r})
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)
"""


def _stub(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "stub.py"
    script.write_text(_LOAD_WORKER + textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, "-I", "-B", str(script)]


def _run(cmd: list[str], payload: bytes = b"", *, timeout: float = 20, cap: int = 2**20) -> bytes:
    return pr.run_child(cmd, payload, timeout=timeout, max_output=cap)


def test_child_memory_growth_is_capped(tmp_path: Path) -> None:
    cmd = _stub(
        tmp_path,
        """
        w.apply_early_limits(30)
        import pypdfium2
        w.apply_late_limits(64 * 2**20)
        try:
            big = bytearray(768 * 2**20)
            sys.stdout.write("ALLOCATED")
        except MemoryError:
            sys.stdout.write("MEMORY-CAPPED")
        """,
    )
    assert _run(cmd) == b"MEMORY-CAPPED"


def test_child_cpu_time_is_capped(tmp_path: Path) -> None:
    cmd = _stub(
        tmp_path,
        """
        w.apply_early_limits(1)
        while True:
            pass
        """,
    )
    started = time.monotonic()
    with pytest.raises(pr.ChildFailedError) as exc:
        _run(cmd, timeout=20)
    assert time.monotonic() - started < 8, "the CPU cap, not the wall clock, stopped it"
    assert exc.value.reason.startswith("exit -")


def test_child_cannot_write_files(tmp_path: Path) -> None:
    cmd = _stub(
        tmp_path,
        """
        w.apply_early_limits(10)
        try:
            with open("leak.bin", "wb") as fh:
                fh.write(b"x" * 4096)
            sys.stdout.write("WROTE")
        except OSError:
            sys.stdout.write("NO-WRITE")
        """,
    )
    assert _run(cmd) == b"NO-WRITE"


def test_child_cannot_open_sockets_or_files_after_the_late_limits(tmp_path: Path) -> None:
    cmd = _stub(
        tmp_path,
        """
        import socket
        w.apply_early_limits(10)
        w.apply_late_limits(64 * 2**20)
        out = []
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            out.append("SOCKET")
        except OSError:
            out.append("NO-SOCKET")
        try:
            open("/etc/hostname", "rb")
            out.append("OPENED")
        except OSError:
            out.append("NO-OPEN")
        os.close(0)  # a freed low descriptor must not be reusable either
        try:
            open("/proc/self/status", "rb")
            out.append("REOPENED")
        except OSError:
            out.append("NO-REOPEN")
        sys.stdout.write(",".join(out))
        """,
    )
    assert _run(cmd) == b"NO-SOCKET,NO-OPEN,NO-REOPEN"


def test_child_gets_an_empty_environment_and_a_removed_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_SECRET_PROBE", "s3cret")
    cmd = _stub(
        tmp_path,
        """
        import json
        sys.stdout.write(json.dumps({"env": sorted(os.environ), "cwd": os.getcwd()}))
        """,
    )
    out = json.loads(_run(cmd))
    assert set(out["env"]) <= {"PATH", "HOME", "TMPDIR", "LC_ALL", "LC_CTYPE"}
    assert "ATLAS_SECRET_PROBE" not in out["env"]
    assert "DATABASE_URL" not in out["env"] and "JWT_SECRET" not in out["env"]
    assert "atlas-pdf-" in out["cwd"]
    assert not Path(out["cwd"]).exists(), "the temporary directory is removed"


def _gone_or_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] in {"Z", "X"}


def test_timeout_kills_the_whole_process_group_no_orphans(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    cmd = _stub(
        tmp_path,
        f"""
        import subprocess, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open({str(pidfile)!r}, "w") as fh:
            fh.write(f"{{os.getpid()}} {{child.pid}}")
        time.sleep(60)
        """,
    )
    started = time.monotonic()
    with pytest.raises(pr.ChildFailedError) as exc:
        _run(cmd, timeout=1.5)
    assert exc.value.reason == "timeout"
    assert time.monotonic() - started < 6
    pids = [int(p) for p in pidfile.read_text().split()]
    assert len(pids) == 2
    deadline = time.monotonic() + 3
    while not all(_gone_or_zombie(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(_gone_or_zombie(p) for p in pids), "the child and its grandchild are killed"


def test_output_is_bounded(tmp_path: Path) -> None:
    cmd = _stub(
        tmp_path,
        """
        while True:
            sys.stdout.buffer.write(b"x" * 65536)
        """,
    )
    started = time.monotonic()
    with pytest.raises(pr.ChildFailedError) as exc:
        _run(cmd, cap=256 * 1024, timeout=10)
    assert exc.value.reason == "output too large"
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "body",
    ["os.abort()", "sys.exit(3)", "import ctypes; ctypes.string_at(0)"],
    ids=["abort", "exit-3", "segfault"],
)
def test_crashes_and_non_zero_exits_are_failures(tmp_path: Path, body: str) -> None:
    cmd = _stub(tmp_path, "sys.stdout.write('partial'); sys.stdout.flush()\n" + body)
    with pytest.raises(pr.ChildFailedError) as exc:
        _run(cmd)
    assert exc.value.reason.startswith("exit ")


def test_large_payload_round_trips_without_deadlock(tmp_path: Path) -> None:
    payload = os.urandom(6 * 2**20)
    cmd = _stub(
        tmp_path,
        """
        data = sys.stdin.buffer.read()
        sys.stdout.buffer.write(data[::-1])
        """,
    )
    assert _run(cmd, payload, cap=7 * 2**20) == payload[::-1]


def test_child_that_ignores_stdin_still_answers(tmp_path: Path) -> None:
    cmd = _stub(tmp_path, "sys.stdout.write('early')\n")
    assert _run(cmd, os.urandom(4 * 2**20)) == b"early"


# ---------------------------------------------------------------------------
# 5. The answer framing, with fake renderers
# ---------------------------------------------------------------------------


def _frame(header: dict[str, Any], body: bytes = b"") -> bytes:
    raw = json.dumps(header).encode()
    return pr.MAGIC + struct.pack(">I", len(raw)) + raw + body


def _fake_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bytes) -> None:
    out = tmp_path / "answer.bin"
    out.write_bytes(answer)
    cmd = _stub(
        tmp_path,
        f"""
        sys.stdin.buffer.read()
        sys.stdout.buffer.write(open({str(out)!r}, "rb").read())
        """,
    )
    monkeypatch.setattr(pr, "worker_command", lambda _limits: cmd)


def _a_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (30, 40), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_fake_renderer_success_is_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_worker(tmp_path, monkeypatch, _frame({"pages_total": 7, "pages_used": 3}, _a_png()))
    out = pr.rasterize_pdf(b"%PDF-1.7", _limits())
    assert (out.pages_total, out.pages_used, out.png) == (7, 3, _a_png())


@pytest.mark.parametrize(
    "answer",
    [
        b"",
        b"garbage",
        pr.MAGIC + struct.pack(">I", 999_999) + b"{}",
        pr.MAGIC + struct.pack(">I", 3) + b"not",
        _frame({"pages_total": 7, "pages_used": 5}, b"x"),  # more than max_pages
        _frame({"pages_total": 2, "pages_used": 3}, b"x"),  # more than the PDF has
        _frame({"pages_total": 1, "pages_used": 0}, b"x"),
        _frame({"pages_total": "1", "pages_used": 1}, b"x"),
        _frame({"pages_total": 1, "pages_used": 1}),  # no image
    ],
)
def test_malformed_answers_are_child_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bytes
) -> None:
    _fake_worker(tmp_path, monkeypatch, answer)
    with pytest.raises(pr.ChildFailedError):
        pr.rasterize_pdf(b"%PDF-1.7", _limits())


def test_worker_error_codes_are_render_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_worker(tmp_path, monkeypatch, _frame({"error": "encrypted"}))
    with pytest.raises(pr.PdfRenderError) as exc:
        pr.rasterize_pdf(b"%PDF-1.7", _limits())
    assert exc.value.code == "encrypted"


def test_a_rendered_image_that_fails_the_image_checks_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's PNG goes through the normal image pipeline: a fake 'PNG'
    that is not one is refused like any bad upload (422, not stored)."""
    _fake_worker(
        tmp_path, monkeypatch, _frame({"pages_total": 1, "pages_used": 1}, b"%PDF-1.7 not a png")
    )
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(f.text_pdf(1))
    assert exc.value.message == g.PDF_UNREADABLE_MESSAGE


# ---------------------------------------------------------------------------
# 6. The guard maps every renderer failure to a 422
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (pr.PdfRenderError("encrypted"), g.PDF_ENCRYPTED_MESSAGE),
        (pr.PdfRenderError("no_pages"), g.PDF_NO_PAGES_MESSAGE),
        (pr.PdfRenderError("unreadable"), g.PDF_UNREADABLE_MESSAGE),
        (pr.PdfRenderError("too_large_output"), g.PDF_UNREADABLE_MESSAGE),
        (pr.PdfRenderError("something-new"), g.PDF_UNREADABLE_MESSAGE),
        (pr.ChildFailedError("timeout"), g.PDF_UNREADABLE_MESSAGE),
        (pr.ChildFailedError("exit -11"), g.PDF_UNREADABLE_MESSAGE),
        (OSError("no python"), g.PDF_UNREADABLE_MESSAGE),
    ],
)
def test_renderer_failures_are_422(
    monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    def fail(*_a: Any, **_k: Any) -> Any:
        raise error

    monkeypatch.setattr(pr, "rasterize_pdf", fail)
    with pytest.raises(g.InvalidUploadError) as exc:
        _inspect(f.text_pdf(1))
    assert exc.value.status_code == 422
    assert exc.value.message == message


def test_pdf_claims_without_pdf_bytes_are_415() -> None:
    png = _a_png()
    with pytest.raises(g.UnsupportedMediaTypeError) as exc:
        _inspect(png, "receipt.pdf", "application/pdf")
    assert exc.value.message == g.PDF_MISMATCH_MESSAGE


@pytest.mark.parametrize(
    ("name", "ctype"),
    [("scan.png", None), (None, "image/png"), ("x.jpg", "application/pdf"), (None, "text/html")],
)
def test_pdf_bytes_with_a_lying_name_or_type_are_415(name: str | None, ctype: str | None) -> None:
    with pytest.raises(g.UnsupportedMediaTypeError):
        _inspect(f.text_pdf(1), name, ctype)
