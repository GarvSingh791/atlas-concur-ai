"""CSV export helpers (Product gap 8) — RFC-4180, injection-safe, capped, streamed.

Shared by the finance exports (GET /approvals/export.csv, GET /audit/export.csv).

* RFC 4180: CRLF record separator, fields quoted when they contain a comma, a
  double quote, CR or LF, embedded quotes doubled (``csv`` with QUOTE_MINIMAL).
* UTF-8 with a BOM so spreadsheet tools detect the encoding (names may be
  non-ASCII).
* CSV / formula injection (OWASP): a text cell starting with ``=``, ``+``,
  ``-``, ``@``, TAB or CR is prefixed with a single quote so a spreadsheet
  shows it as text instead of evaluating it.  Every *text* cell goes through
  :func:`sanitize_cell`; only cells the server formats itself from a Decimal /
  datetime / bool are emitted as-is (they cannot carry a formula).
* Hard row cap: callers fetch ``cap + 1`` rows; when more rows than the cap
  exist, the file ends with a marker row and the response carries
  ``X-Export-Truncated: true`` so a truncated file is never mistaken for a
  complete one.
* The body is rendered from rows already loaded inside the request (the DB
  session is closed by the time a streaming body is sent) and streamed out in
  chunks.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.responses import StreamingResponse

# Hard cap on exported data rows (module constant; tests monkeypatch it).
EXPORT_MAX_ROWS = 5000

_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
TRUNCATION_MARKER = "# TRUNCATED"
_BOM = "﻿"
_CHUNK_ROWS = 500


class Raw(str):
    """A server-formatted cell (number / timestamp / flag) that needs no sanitising."""


Cell = str | Raw | None


def sanitize_cell(value: object) -> str:
    """Neutralise spreadsheet formula injection in a text cell."""
    if value is None:
        return ""
    text = str(value)
    if isinstance(value, Raw):
        return text
    if text.startswith(_INJECTION_PREFIXES):
        return "'" + text
    return text


def money_cell(value: Decimal | None) -> Raw:
    return Raw("" if value is None else f"{value:.2f}")


def timestamp_cell(value: datetime | date | None) -> Raw:
    if value is None:
        return Raw("")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return Raw(value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return Raw(value.isoformat())


def bool_cell(value: bool) -> Raw:
    return Raw("yes" if value else "no")


def _render(rows: Iterable[Sequence[object]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    for row in rows:
        writer.writerow([sanitize_cell(c) for c in row])
    return buf.getvalue()


def iter_csv(
    header: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    truncated: bool,
    cap: int,
) -> Iterator[str]:
    """Yield the CSV body in chunks: BOM + header, data rows, optional marker row."""
    yield _BOM + _render([header])
    for start in range(0, len(rows), _CHUNK_ROWS):
        yield _render(rows[start : start + _CHUNK_ROWS])
    if truncated:
        yield _render(
            [
                [
                    TRUNCATION_MARKER,
                    f"Export limited to {cap} rows; narrow the filters to export the rest.",
                ]
            ]
        )


def export_filename(stem: str, today: date | None = None) -> str:
    day = today or datetime.now(timezone.utc).date()
    return f"atlas-{stem}-{day.isoformat()}.csv"


def csv_response(
    *,
    filename: str,
    header: Sequence[str],
    rows: Sequence[Sequence[object]],
    truncated: bool,
    cap: int,
) -> StreamingResponse:
    """Stream a rendered CSV as a download with truncation + count headers."""
    return StreamingResponse(
        (chunk.encode("utf-8") for chunk in iter_csv(header, rows, truncated=truncated, cap=cap)),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Row-Count": str(len(rows)),
            "X-Export-Truncated": "true" if truncated else "false",
            "Cache-Control": "no-store",
        },
    )
