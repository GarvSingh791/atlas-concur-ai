"""Rate specs for the request limiter (phase 11).

A rate is written ``"<count>/<unit>"`` with unit ``second``, ``minute``,
``hour`` or ``day`` (plural and abbreviated forms accepted: ``s``, ``min``,
``h``, ``d``).  A ``day`` window is a FIXED UTC day — a daily quota resets at
00:00 UTC — while shorter windows slide.  Kept dependency-free so the settings
layer can validate the specs at startup and the limiter can reuse the parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
}

_SPEC = re.compile(r"^\s*(\d{1,9})\s*/\s*([a-z]+?)s?\s*$")

DAY_SECONDS = 86400.0


@dataclass(frozen=True)
class Rate:
    """``limit`` requests per ``window_seconds``; ``fixed`` = calendar-aligned window."""

    limit: int
    window_seconds: float
    fixed: bool = False

    def describe(self) -> str:
        return f"{self.limit}/{int(self.window_seconds)}s{' (fixed UTC day)' if self.fixed else ''}"


def parse_rate(spec: str) -> Rate:
    """Parse ``"60/hour"`` into a :class:`Rate`; raise ``ValueError`` when invalid."""
    match = _SPEC.match(str(spec).lower())
    if match is None:
        raise ValueError(f"invalid rate {spec!r}: expected '<count>/<second|minute|hour|day>'")
    limit = int(match.group(1))
    unit = match.group(2)
    window = _UNITS.get(unit)
    if window is None:
        raise ValueError(f"invalid rate {spec!r}: unknown unit {unit!r}")
    if limit < 1:
        raise ValueError(f"invalid rate {spec!r}: the count must be at least 1")
    return Rate(limit=limit, window_seconds=window, fixed=window >= DAY_SECONDS)
