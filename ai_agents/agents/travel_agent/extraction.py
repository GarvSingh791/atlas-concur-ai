"""Free-text trip slot extraction + cross-turn merge for the Travel Agent.

The planner's chips are the structured intent, but users type sentences
("Fly from Chicago to New York next Tuesday for 3 days, client meeting") and
answer clarify questions with fragments ("Chicago (ORD)"). This module turns the
message text into slot values and merges them onto the intent already known
for the conversation:

    prior turn intent  <  chips sent in the payload  <  values stated in the text

Extraction is two-layered and safe by construction:

  * a deterministic parser (regex + the mock-inventory location table) that
    always runs — it is the only extractor in local/dev/CI mode and the one that
    resolves relative dates ("next Tuesday", "tomorrow", "in 3 days") against
    an injected ``today``;
  * an optional LLM extractor (real providers only) that returns a JSON object.
    Its output is parsed tolerantly (code fences, prose, alias keys, "null"
    strings), every value is re-validated, and any failure falls back to the
    deterministic result. Deterministic dates win over LLM dates (date math
    stays deterministic); the LLM wins for locations/purpose/travelers, which
    it reads more robustly.

Ranking never sees the LLM — only the merged, validated slot values.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from services.llm_provider.interface import LlmMessage, LlmProvider
from services.llm_provider.local import LocalLlmProvider
from services.mock_inventory.locations import known_location_names, resolve_airport

logger = logging.getLogger(__name__)

# Slots this module reads/writes (TripIntent field names).
SLOT_FIELDS: tuple[str, ...] = (
    "origin",
    "destination",
    "departure_date",
    "return_date",
    "travelers",
    "currency",
    "purpose",
)
_REQUIRED_ORDER: tuple[str, ...] = (
    "origin",
    "destination",
    "departure_date",
    "travelers",
    "currency",
)
# Optional slots a client may clear by sending an explicit null.
_CLEARABLE: frozenset[str] = frozenset({"return_date", "purpose"})
_DATE_SLOTS: frozenset[str] = frozenset({"departure_date", "return_date"})

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
_MONTHS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_NUMBER_WORDS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "couple of": 2,
    "a couple of": 2,
}
# Product gap 8: "a few" = 3, recognised ONLY in relative date shifts ("a few days
# later", "a few more nights") — never as a headcount or an in-N-days date.
# "several" is deliberately NOT a number: it is too vague to guess, so the planner
# asks how many days instead (``_VAGUE_SEVERAL``).
_FEW_WORDS: dict[str, int] = {"a few": 3}
_CURRENCY_CODES: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "INR", "SGD"}
)
_CURRENCY_WORDS: dict[str, str] = {
    "dollars": "USD",
    "us dollars": "USD",
    "euros": "EUR",
    "euro": "EUR",
    "pounds": "GBP",
    "sterling": "GBP",
}
_PURPOSE_NOUNS = (
    "meeting",
    "meetings",
    "conference",
    "summit",
    "workshop",
    "offsite",
    "off-site",
    "training",
    "kickoff",
    "kick-off",
    "site visit",
    "client visit",
    "customer visit",
    "trade show",
    "interview",
    "onsite",
    "presentation",
    "pitch",
    "audit",
)

_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NUM_ALT = r"\d{1,2}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
# Shift counts: the numbers above plus "a few" (tried before the bare "a").
_SHIFT_NUM_ALT = "|".join(_FEW_WORDS) + "|" + _NUM_ALT

_RETURN_CUES = re.compile(
    r"(return(?:ing)?|back|until|till|through|thru|check(?:ing)?[- ]?out|"
    r"coming home|fly home|home|return date(?: is)?)\W*(?:on|by)?\W*$",
    re.I,
)


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    value: Any
    # Bare / "this|on|coming" weekday -> its weekday number (re-anchorable), else None.
    weekday: int | None = None


# ---------------------------------------------------------------------------
# Date parsing (deterministic, relative to an injected today)
# ---------------------------------------------------------------------------


def _number(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token, _FEW_WORDS.get(token))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _yearless(month: int, day: int, today: date) -> date | None:
    """A month/day without a year: the next occurrence on/after today."""
    d = _safe_date(today.year, month, day)
    if d is not None and d < today:
        d = _safe_date(today.year + 1, month, day)
    return d


def _parse_iso(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _weekday_date(weekday: int, qualifier: str | None, today: date) -> date:
    """Resolve a weekday name against today.

    "next <weekday>"  -> that weekday in the following calendar week (Mon-start),
                         e.g. on Wed 2026-11-11 "next Tuesday" = 2026-11-17 and
                         "next Thursday" = 2026-11-19.
    "<weekday>" / "this|on|coming <weekday>" -> the next occurrence after today.
    """
    if qualifier and qualifier.lower() == "next":
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=weekday)
    delta = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "iso"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"), "slash"),
    (
        re.compile(
            rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
            rf"(?:\s*(?:-|–|to|through|until)\s*(\d{{1,2}})(?:st|nd|rd|th)?)?"
            rf"(?:,?\s*(\d{{4}}))?\b",
            re.I,
        ),
        "month_day",
    ),
    (
        re.compile(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b\.?(?:,?\s*(\d{{4}}))?",
            re.I,
        ),
        "day_month",
    ),
    (re.compile(r"\b(day after tomorrow|tomorrow|today|tonight)\b", re.I), "word"),
    (re.compile(rf"\b(?:(next|this|coming|on)\s+)?({_WEEKDAY_ALT})\b\.?", re.I), "weekday"),
    (re.compile(rf"\bin\s+({_NUM_ALT})\s+(days?|weeks?|fortnights?)\b", re.I), "in_n"),
)


def _find_dates(text: str, today: date) -> list[_Span]:
    """Return non-overlapping date mentions (each value is a list of 1-2 dates)."""
    found: list[_Span] = []

    def overlaps(s: int, e: int) -> bool:
        return any(not (e <= f.start or s >= f.end) for f in found)

    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            dates: list[date] = []
            if kind == "iso":
                d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                dates = [d] if d else []
            elif kind == "slash":
                month, day = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    year = int(m.group(3))
                    year = year + 2000 if year < 100 else year
                    d = _safe_date(year, month, day)
                else:
                    d = _yearless(month, day, today)
                dates = [d] if d else []
            elif kind == "month_day":
                month = _MONTHS[m.group(1).lower()]
                first = (
                    _safe_date(int(m.group(4)), month, int(m.group(2)))
                    if m.group(4)
                    else _yearless(month, int(m.group(2)), today)
                )
                if first:
                    dates = [first]
                    if m.group(3):
                        second = _safe_date(first.year, month, int(m.group(3)))
                        if second and second > first:
                            dates.append(second)
            elif kind == "day_month":
                month = _MONTHS[m.group(2).lower()]
                day = int(m.group(1))
                d = (
                    _safe_date(int(m.group(3)), month, day)
                    if m.group(3)
                    else _yearless(month, day, today)
                )
                dates = [d] if d else []
            elif kind == "word":
                word = m.group(1).lower()
                offset = {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2}[word]
                dates = [today + timedelta(days=offset)]
            elif kind == "weekday":
                # "sat"/"sun"/"wed" etc. only count with a qualifier or as a full name,
                # so "sun" in prose is not misread.
                name = m.group(2).lower()
                if m.group(1) is None and len(name) <= 4:
                    continue
                dates = [_weekday_date(_WEEKDAYS[name], m.group(1), today)]
                qualifier = (m.group(1) or "").lower()
                if qualifier != "next":
                    found.append(_Span(m.start(), m.end(), dates, weekday=_WEEKDAYS[name]))
                    continue
            elif kind == "in_n":
                n = _number(m.group(1))
                if n is not None:
                    dates = [today + timedelta(days=n * _unit_days(m.group(2)))]
            if dates:
                found.append(_Span(m.start(), m.end(), dates))
    return sorted(found, key=lambda s: s.start)


_DURATION_RE = re.compile(
    rf"\b(?:for\s+)?({_NUM_ALT})[\s-]+(days?|nights?|weeks?|fortnights?)\b(?:\s+trip)?",
    re.I,
)


# ── Relative shifts of a KNOWN date (UAT2 defect K) ─────────────────────────
# "come back a day later" / "stay an extra night" / "leave a day earlier" move the
# prior intent's return / departure date; they are not trip lengths ("a day" used
# to be read as a 1-day trip, collapsing the return onto the departure).
_SHIFT_UNIT = r"(days?|nights?|weeks?|fortnights?)"
_SHIFT_LATER_EARLIER = re.compile(
    rf"\b({_SHIFT_NUM_ALT})[\s-]+{_SHIFT_UNIT}\s+(later|earlier|sooner)\b", re.I
)
_SHIFT_EXTRA = re.compile(
    rf"\b(?:({_SHIFT_NUM_ALT})\s+(?:extra|more|additional)|another)\s+(days?|nights?)\b", re.I
)
_SHIFT_FEWER = re.compile(
    rf"\b({_SHIFT_NUM_ALT})\s+(?:(?:fewer|less)\s+(days?|nights?)|(days?|nights?)\s+(?:fewer|less|shorter))\b",
    re.I,
)
_SHIFT_EXTEND = re.compile(
    rf"\b(extend\w*|shorten\w*|cut)\b[^.;]{{0,30}}?\bby\s+({_SHIFT_NUM_ALT})\s+{_SHIFT_UNIT}\b",
    re.I,
)
# Product gap 8: phrases too vague to turn into a date.  They are never guessed
# (by the parser or the LLM): the prior dates are kept and the planner asks.
_VAGUE_SEVERAL = re.compile(
    r"\bseveral\s+(?:more\s+|extra\s+|additional\s+)?(?:days?|nights?|weeks?)\b", re.I
)
_VAGUE_NEXT_WEEK = re.compile(r"\bnext\s+week\b", re.I)  # "next weekend" never matches
_DEPART_SHIFT_CUE = re.compile(
    r"\b(leav(?:e|ing)|depart(?:ing|ure)?|fly(?:ing)?\s+out|head(?:ing)?\s+out|"
    r"set(?:ting)?\s+off|go(?:ing)?|outbound|start(?:ing)?|arriv(?:e|ing)|get(?:ting)?\s+there)\b",
    re.I,
)
_RETURN_SHIFT_CUE = re.compile(
    r"\b(back|return(?:ing)?|home|stay(?:ing)?|check(?:ing)?[- ]?out|inbound|way\s+back)\b",
    re.I,
)
# An explicit same-day return: "a day trip", "same-day return", "back the same day".
# "day trip" glued to a count ("3-day trip", "five day trip", "multi-day trip") is
# a trip LENGTH, not a same-day return — see _same_day_requested.
_SAME_DAY_RE = re.compile(
    r"(?<!\w)day[- ]trip\b|\bsame[- ]day\s+(?:return|trip|round[- ]?trip)\b|"
    r"\b(?:back|return\w*|home)\b(?:\W+\w+){0,3}?\W+same\s+day\b",
    re.I,
)
# The word right before "day trip" when it counts days: "3 day trip", "five day trip",
# "a few day trip". A count of one ("a one-day trip") is still a same-day return.
_DAY_TRIP_COUNT_BEFORE = re.compile(
    rf"\b(?:({_NUM_ALT})|few|couple|several|many|multiple|multi)[\s-]*$", re.I
)
# A negation earlier in the SAME clause: "I don't want to come back the same day",
# "it's not a day trip", "no same-day return".
_SAME_DAY_NEGATION = re.compile(
    r"\b(?:not|no|never|without|rather\s+than|instead\s+of)\b|n't\b|n’t\b", re.I
)
_CLAUSE_BREAK = re.compile(r"[,.;:!?]|\b(?:but|so|and\s+then)\b", re.I)


def _same_day_requested(text: str) -> bool:
    """True when the message explicitly asks for a same-day return.

    Skips "N-day trip" (a trip length: "3-day trip", "five day trip") and negated
    phrasings ("I don't want to come back the same day", "not a day trip").
    """
    for m in _SAME_DAY_RE.finditer(text):
        before = text[: m.start()]
        if m.group(0).lower().startswith("day"):
            counted = _DAY_TRIP_COUNT_BEFORE.search(before)
            if counted and (counted.group(1) is None or (_number(counted.group(1)) or 0) > 1):
                continue
        breaks = list(_CLAUSE_BREAK.finditer(before))
        clause = before[breaks[-1].end() :] if breaks else before
        if _SAME_DAY_NEGATION.search(clause):
            continue
        return True
    return False


def _unit_days(unit: str) -> int:
    unit = unit.lower()
    if unit.startswith("fortnight"):
        return 14
    return 7 if unit.startswith("week") else 1


def _shift_slot(text: str, start: int, floor: int) -> str:
    """Which date a "N days later|earlier" phrase moves: the cue nearest before it."""
    window = text[floor:start]
    dep = [m.end() for m in _DEPART_SHIFT_CUE.finditer(window)]
    ret = [m.end() for m in _RETURN_SHIFT_CUE.finditer(window)]
    if dep and (not ret or dep[-1] > ret[-1]):
        return "departure_date"
    return "return_date"  # the date follow-ups move most often


def _find_shifts(text: str) -> tuple[dict[str, int], list[tuple[int, int]]]:
    """Relative date shifts in ``text`` -> ({slot: days}, matched spans)."""
    found: list[tuple[int, int, str, int]] = []  # (start, end, slot, days)

    def free(s: int, e: int) -> bool:
        return all(e <= f[0] or s >= f[1] for f in found)

    for m in _SHIFT_EXTEND.finditer(text):
        n = _number(m.group(2))
        if n and free(m.start(), m.end()):
            sign = 1 if m.group(1).lower().startswith("extend") else -1
            found.append((m.start(), m.end(), "return_date", sign * n * _unit_days(m.group(3))))
    for m in _SHIFT_EXTRA.finditer(text):
        n = _number(m.group(1)) if m.group(1) else 1
        if n and free(m.start(), m.end()):
            found.append((m.start(), m.end(), "return_date", n))
    for m in _SHIFT_FEWER.finditer(text):
        n = _number(m.group(1))
        if n and free(m.start(), m.end()):
            found.append((m.start(), m.end(), "return_date", -n))
    for m in _SHIFT_LATER_EARLIER.finditer(text):
        n = _number(m.group(1))
        if not n or not free(m.start(), m.end()):
            continue
        sign = 1 if m.group(3).lower() == "later" else -1
        floor = max((f[1] for f in found if f[1] <= m.start()), default=0)
        slot = _shift_slot(text, m.start(), floor)
        found.append((m.start(), m.end(), slot, sign * n * _unit_days(m.group(2))))

    shifts: dict[str, int] = {}
    for _s, _e, slot, days in found:
        shifts[slot] = shifts.get(slot, 0) + days
    return {k: v for k, v in shifts.items() if v}, [(s, e) for s, e, _sl, _d in found]


def _find_duration(text: str) -> tuple[int, str] | None:
    """Return (count, unit) for "for 3 days" / "2 nights" / "a 3-day trip"."""
    for m in _DURATION_RE.finditer(text):
        # "in 3 days" is a relative date, not a trip length.
        prefix = text[max(0, m.start() - 4) : m.start()].lower()
        if prefix.rstrip().endswith("in"):
            continue
        n = _number(m.group(1))
        if n is None or n <= 0:
            continue
        unit = m.group(2).lower()
        if unit.startswith("fortnight"):
            return 2 * n, "week"
        unit = "night" if unit.startswith("night") else "week" if unit.startswith("week") else "day"
        return n, unit
    return None


def return_from_duration(departure: str, count: int, unit: str) -> str | None:
    """Return date for a trip length. N nights -> +N; N weeks -> +7N; N days -> +(N-1).

    "3 days" departing Tuesday = Tue, Wed, Thu -> back Thursday (2 hotel nights).
    """
    try:
        dep = date.fromisoformat(departure)
    except (TypeError, ValueError):
        return None
    if unit == "night":
        delta = count
    elif unit == "week":
        delta = 7 * count
    else:
        delta = max(count - 1, 0)
    return (dep + timedelta(days=delta)).isoformat()


def _parse_date_value(raw: Any, today: date) -> str | None:
    """Validate/resolve a date value from the LLM or a client (ISO or a phrase)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    spans = _find_dates(text, today)
    if spans:
        first: date = spans[0].value[0]
        return first.isoformat()
    return None


# ---------------------------------------------------------------------------
# Location / travelers / currency / purpose
# ---------------------------------------------------------------------------

_LOCATION_NAMES = known_location_names()
_LOCATION_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(n) for n in _LOCATION_NAMES) + r")(?![A-Za-z])",
    re.I,
)
_CAPS_PLACE_RE = re.compile(
    r"\b(from|to)\s+([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3})(?:\s*\(([A-Z]{3})\))?"
)
_ORIGIN_CUE = re.compile(r"(from|leaving|departing|out of)\W*$", re.I)
_DEST_CUE = re.compile(r"(\bto|into|→|->|-|–|visit(?:ing)?|destination(?: is)?)\W*$", re.I)
_IN_CUE = re.compile(r"\b(in|at)\W*$", re.I)
_AREA_CUE = re.compile(r"\b(near|around|close to|by)\W*$", re.I)
_NOT_PLACES = set(_WEEKDAYS) | set(_MONTHS) | {"the", "a", "an", "my", "our"}


def _find_locations(text: str) -> list[tuple[int, str, str | None]]:
    """Return (position, location, role) mentions; role in {origin, destination, in, None}."""
    spans: list[_Span] = []
    for m in _LOCATION_RE.finditer(text):
        if any(m.start() < s.end for s in spans):
            continue  # the "(ORD)" tail of an already-captured "Chicago (ORD)"
        raw = m.group(1)
        # Bare 3-letter codes must be typed as codes (upper-case) to avoid prose hits.
        if len(raw) == 3 and raw.lower() != "nyc" and not raw.isupper():
            continue
        end = m.end()
        tail = re.match(r"\s*\(([A-Z]{3})\)", text[end:])
        if tail:
            end += tail.end()
        spans.append(_Span(m.start(), end, text[m.start() : end]))
    for m in _CAPS_PLACE_RE.finditer(text):
        start = m.start(2)
        end = m.end()
        if any(not (end <= s.start or start >= s.end) for s in spans):
            continue
        words = m.group(2).split()
        while words and words[-1].lower().strip(".") in _NOT_PLACES:
            words.pop()
        if not words or words[0].lower() in _NOT_PLACES:
            continue
        value = " ".join(words)
        if m.group(3):
            value = f"{value} ({m.group(3)})"
        spans.append(_Span(start, end, value))

    mentions: list[tuple[int, str, str | None]] = []
    for span in sorted(spans, key=lambda s: s.start):
        before = text[max(0, span.start - 16) : span.start]
        role: str | None
        if _AREA_CUE.search(before):
            continue  # "a hotel near Midtown" is a neighbourhood, not a trip leg
        if _ORIGIN_CUE.search(before):
            role = "origin"
        elif _DEST_CUE.search(before):
            role = "destination"
        elif _IN_CUE.search(before):
            role = "in"
        else:
            role = None
        mentions.append((span.start, str(span.value), role))
    return mentions


_TRAVELERS_RE = re.compile(
    rf"\b({_NUM_ALT})\s+(?:travel(?:l)?ers?|people|persons|passengers|adults|colleagues|of us)\b",
    re.I,
)
_SOLO_RE = re.compile(r"\b(just me|only me|myself|by myself|solo|alone)\b", re.I)
_PURPOSE_RE = re.compile(
    r"\b((?:[A-Za-z-]+\s+){0,2}(?:" + "|".join(re.escape(n) for n in _PURPOSE_NOUNS) + r"))\b",
    re.I,
)
_PURPOSE_STOP = {
    "for",
    "a",
    "an",
    "the",
    "to",
    "attend",
    "attending",
    "and",
    "with",
    "days",
    "day",
    "nights",
    "trip",
    "my",
    "our",
    "of",
    "on",
    "in",
}


def _find_purpose(text: str) -> str | None:
    m = _PURPOSE_RE.search(text)
    if not m:
        return None
    words = m.group(1).split()
    while words and words[0].lower() in _PURPOSE_STOP:
        words.pop(0)
    if not words:
        return None
    phrase = " ".join(words)
    return phrase[0].upper() + phrase[1:]


def parse_message(text: str, *, today: date, pending_slot: str | None = None) -> dict[str, Any]:
    """Deterministically extract the slots a single message states.

    ``pending_slot`` is the slot the previous clarify question asked for; a bare
    answer ("Chicago (ORD)", "2") is assigned to it.
    """
    out: dict[str, Any] = {}
    if not text or not text.strip():
        return out

    # ── Locations ────────────────────────────────────────────────────────────
    unassigned: list[str] = []
    in_mentions: list[str] = []
    for _pos, loc, role in _find_locations(text):
        if role in ("origin", "destination") and role not in out:
            out[role] = loc
        elif role == "in":
            in_mentions.append(loc)
        elif role is None:
            unassigned.append(loc)
    for loc in unassigned:
        if pending_slot in ("origin", "destination") and pending_slot not in out:
            out[pending_slot] = loc
        elif "origin" not in out and ("destination" in out or len(unassigned) > 1):
            out["origin"] = loc
        elif "destination" not in out:
            out["destination"] = loc
    if "destination" not in out and in_mentions:
        out["destination"] = in_mentions[0]

    # ── Dates ────────────────────────────────────────────────────────────────
    departure: date | None = None
    return_: date | None = None
    return_weekday: int | None = None
    for span in _find_dates(text, today):
        dates: list[date] = span.value
        before = text[max(0, span.start - 24) : span.start]
        if len(dates) == 2:
            departure, return_ = departure or dates[0], return_ or dates[1]
        elif _RETURN_CUES.search(before):
            if return_ is None:
                return_ = dates[0]
                return_weekday = span.weekday
        elif departure is None:
            departure = dates[0]
        elif return_ is None:
            return_ = dates[0]
    if departure is not None:
        out["departure_date"] = departure.isoformat()
    if return_ is not None:
        out["return_date"] = return_.isoformat()
        if return_weekday is not None:
            # "come back Friday": resolved from today here; resolve_slots re-anchors
            # it after the trip's departure when today's Friday is before it (M3).
            out["_return_weekday"] = return_weekday
    shifts, shift_spans = _find_shifts(text)
    if shifts:
        out["_shift"] = shifts
    if _same_day_requested(text):
        out["_same_day"] = True
    # A shift phrase ("a day later", "one night less") is never a trip length.
    unshifted = text
    for s, e in shift_spans:
        unshifted = unshifted[:s] + " " * (e - s) + unshifted[e:]
    duration = _find_duration(unshifted)
    if duration is not None:
        out["_duration"] = duration
    # Product gap 8: "several days later" / a bare "next week" are not guessed.
    if _VAGUE_SEVERAL.search(text):
        out["_vague"] = "several"
    elif (
        _VAGUE_NEXT_WEEK.search(text)
        and "departure_date" not in out
        and "return_date" not in out
        and "_shift" not in out
    ):
        out["_vague"] = "next week"

    # ── Travelers / currency / purpose ───────────────────────────────────────
    m = _TRAVELERS_RE.search(text)
    if m and _number(m.group(1)):
        out["travelers"] = _number(m.group(1))
    elif _SOLO_RE.search(text):
        out["travelers"] = 1
    elif pending_slot == "travelers":
        bare = re.fullmatch(r"\s*(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s*\.?\s*", text, re.I)
        if bare and _number(bare.group(1)):
            out["travelers"] = _number(bare.group(1))

    for m in re.finditer(r"\b([A-Za-z]{3})\b", text):
        if m.group(1).upper() in _CURRENCY_CODES and (
            m.group(1).isupper() or pending_slot == "currency"
        ):
            out["currency"] = m.group(1).upper()
            break
    else:
        lower = text.lower()
        for word, code in _CURRENCY_WORDS.items():
            if re.search(rf"\b{word}\b", lower):
                out["currency"] = code
                break

    purpose = _find_purpose(text)
    if purpose:
        out["purpose"] = purpose
    return out


# ---------------------------------------------------------------------------
# LLM extraction (optional; real providers only)
# ---------------------------------------------------------------------------

_LLM_PROMPT = """\
You extract structured trip details from ONE message sent to a corporate travel planner.
Today is {today} ({weekday}).
Trip details already known: {current}
The planner's last question asked for: {pending}

Reply with ONLY a JSON object with exactly these keys (null when the message does not say):
{{"origin": string|null, "destination": string|null, "departure_date": "YYYY-MM-DD"|null,
"return_date": "YYYY-MM-DD"|null, "trip_length_days": integer|null, "nights": integer|null,
"travelers": integer|null, "currency": "ISO 4217 code"|null, "purpose": string|null}}

Rules:
- origin/destination: the city, written as "City (IATA)" when you know its main airport,
  e.g. "Chicago (ORD)". A hotel neighbourhood ("near Midtown") is NOT a destination.
- Resolve relative dates against today. "next <weekday>" means that weekday in the
  following calendar week (weeks start Monday).
- "for N days" -> trip_length_days = N; "N nights" -> nights = N. Do not invent a return date.
- A change RELATIVE to the known dates ("come back a day later", "one more night",
  "leave a day earlier") moves that known date; it is NOT a trip length.
- If the message is just an answer to the planner's last question, put it in that field.
- Only include values the message states; never copy the already-known details.
- purpose: a short phrase such as "Client meeting", or null.

Message: {message}"""

_KEY_ALIASES: dict[str, str] = {
    "from": "origin",
    "origin_city": "origin",
    "departure_city": "origin",
    "departure_airport": "origin",
    "origin_airport": "origin",
    "to": "destination",
    "destination_city": "destination",
    "arrival_city": "destination",
    "destination_airport": "destination",
    "depart_date": "departure_date",
    "departure": "departure_date",
    "start_date": "departure_date",
    "outbound_date": "departure_date",
    "date": "departure_date",
    "return": "return_date",
    "end_date": "return_date",
    "inbound_date": "return_date",
    "duration_days": "trip_length_days",
    "days": "trip_length_days",
    "trip_length": "trip_length_days",
    "num_nights": "nights",
    "num_travelers": "travelers",
    "number_of_travelers": "travelers",
    "passengers": "travelers",
    "travellers": "travelers",
    "trip_purpose": "purpose",
    "reason": "purpose",
}
_NULLISH = {"", "null", "none", "unknown", "n/a", "na", "not specified", "unspecified", "-"}


def parse_llm_json(raw: str) -> dict[str, Any] | None:
    """Parse an LLM extraction reply into a {slot: value} dict, or None.

    Tolerates markdown fences, leading/trailing prose, a wrapping object
    ({"intent": {...}}), alias keys and "null"-like strings.
    """
    if not raw:
        return None
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for wrapper in ("intent", "trip", "slots", "result", "data"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and len(data) == 1:
            data = inner
            break
    out: dict[str, Any] = {}
    for key, value in data.items():
        canon = _KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if isinstance(value, str) and value.strip().lower() in _NULLISH:
            value = None
        if value is not None:
            out[canon] = value
    return out


def _coerce_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        n = int(raw)
    elif isinstance(raw, str):
        m = re.search(r"\d+", raw)
        if m:
            n = int(m.group(0))
        else:
            word = _number(raw)
            if word is None:
                return None
            n = word
    else:
        return None
    return n if 1 <= n <= 365 else None


def normalize_llm_slots(data: Mapping[str, Any], *, today: date) -> dict[str, Any]:
    """Validate parsed LLM values into slot values (drops anything unusable)."""
    out: dict[str, Any] = {}
    for slot in ("origin", "destination"):
        val = data.get(slot)
        if isinstance(val, str) and val.strip():
            out[slot] = val.strip()
    for slot in ("departure_date", "return_date"):
        d = _parse_date_value(data.get(slot), today)
        if d:
            out[slot] = d
    travelers = _coerce_int(data.get("travelers"))
    if travelers is not None and travelers <= 50:
        out["travelers"] = travelers
    cur = data.get("currency")
    if isinstance(cur, str) and cur.strip().upper() in _CURRENCY_CODES:
        out["currency"] = cur.strip().upper()
    purpose = data.get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        phrase = purpose.strip()[:120]
        out["purpose"] = phrase[0].upper() + phrase[1:]
    nights = _coerce_int(data.get("nights"))
    days = _coerce_int(data.get("trip_length_days"))
    if nights is not None:
        out["_duration"] = (nights, "night")
    elif days is not None:
        out["_duration"] = (days, "day")
    return out


def llm_extract(
    text: str,
    *,
    provider: LlmProvider,
    today: date,
    current: Mapping[str, Any],
    pending_slot: str | None,
) -> dict[str, Any] | None:
    """Ask the LLM for slots; None on any provider/parse failure (caller falls back)."""
    prompt = _LLM_PROMPT.format(
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        current=json.dumps({k: v for k, v in current.items() if v is not None}),
        pending=pending_slot or "nothing",
        message=text[:2000],
    )
    try:
        response = provider.complete([LlmMessage(role="user", content=prompt)])
    except Exception as exc:  # ProviderError, timeouts, SDK errors — never fatal
        logger.warning("TravelAgent extraction: LLM call failed (%s); deterministic only.", exc)
        return None
    parsed = parse_llm_json(response.text or "")
    if parsed is None:
        logger.warning("TravelAgent extraction: unparseable LLM reply; deterministic only.")
        return None
    return normalize_llm_slots(parsed, today=today)


def provider_can_extract(provider: LlmProvider | None) -> bool:
    """The local stub returns canned prose, so it never extracts — skip the call."""
    return provider is not None and not isinstance(provider, LocalLlmProvider)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSlots:
    """Result of merging prior intent + payload chips + message text."""

    slots: dict[str, Any]  # final slot values (None when unknown)
    extracted: dict[str, Any]  # what the message text contributed
    source: str  # "llm" | "deterministic" | "none"
    # Set when the message's dates were rejected (the prior dates were kept): the
    # planner asks this instead of searching (UAT2 defect K).
    notice: str | None = None


def first_missing_slot(slots: Mapping[str, Any]) -> str | None:
    for slot in _REQUIRED_ORDER:
        if slots.get(slot) in (None, ""):
            return slot
    return None


def _normalize_location(value: Any) -> Any:
    """Known airport/city -> IATA code; "City (XYZ)" -> "XYZ"; else the text as given."""
    if not isinstance(value, str):
        return value
    code = resolve_airport(value)
    if code:
        return code
    labelled = re.search(r"\(([A-Z]{3})\)", value)
    return labelled.group(1) if labelled else value.strip()


def resolve_slots(
    payload: Mapping[str, Any],
    *,
    text: str | None,
    prior: Mapping[str, Any] | None,
    today: date,
    provider: LlmProvider | None = None,
) -> ResolvedSlots:
    """Merge prior-turn intent < payload chips < message text into final slots."""
    base: dict[str, Any] = {}
    for slot in SLOT_FIELDS:
        if prior and prior.get(slot) not in (None, ""):
            base[slot] = prior[slot]
    for slot in SLOT_FIELDS:
        if slot in payload:
            val = payload[slot]
            if val not in (None, ""):
                base[slot] = val
            elif slot in _CLEARABLE:
                base.pop(slot, None)

    extracted: dict[str, Any] = {}
    explicit_dates: set[str] = set()
    source = "none"
    if text and text.strip():
        pending = first_missing_slot(base)
        det = parse_message(text, today=today, pending_slot=pending)
        explicit_dates = {slot for slot in _DATE_SLOTS if slot in det}
        llm = (
            llm_extract(text, provider=provider, today=today, current=base, pending_slot=pending)
            if provider is not None and provider_can_extract(provider)
            else None
        )
        if llm is not None:
            source = "llm"
            if "_shift" in det or "_same_day" in det or "_vague" in det:
                # "come back a day later" / "a day trip": the dates are computed from
                # the known ones below — the model's absolute dates and trip length
                # for this message are not used (it answered "a day later" with a
                # same-day return in the browser, UAT2 defect K).
                for slot in (*_DATE_SLOTS, "_duration"):
                    if slot not in det:
                        llm.pop(slot, None)
            elif "_duration" in det and "return_date" not in det:
                # "for 3 days": the return date is computed deterministically below,
                # never taken from the model's own date arithmetic.
                llm.pop("return_date", None)
            for slot, val in llm.items():
                if slot in _DATE_SLOTS or slot == "_duration":
                    extracted[slot] = det.get(slot, val)
                else:
                    extracted[slot] = val
            for slot, val in det.items():
                extracted.setdefault(slot, val)
        else:
            extracted = det
            if det:
                source = "deterministic"

    duration = extracted.pop("_duration", None)
    return_weekday = extracted.pop("_return_weekday", None)
    shifts: dict[str, int] = extracted.pop("_shift", None) or {}
    same_day = bool(extracted.pop("_same_day", False))
    vague = extracted.pop("_vague", None)

    # Relative shifts move the KNOWN date (prior turn < chips) and take precedence
    # over any absolute date read for that slot from the same message.
    shifted: set[str] = set()
    for slot in ("departure_date", "return_date"):
        if slot not in shifts:
            continue
        if slot in explicit_dates:
            # "Nov 18 to Nov 20, my meeting starts a day later": a date the user
            # wrote out in this very message wins over a relative phrase.
            continue
        days = shifts[slot]
        anchor = _parse_iso(str(base.get(slot) or ""))
        if slot == "return_date":
            new_dep = extracted.get("departure_date")
            if (
                new_dep
                and "departure_date" not in shifted
                and new_dep != base.get("departure_date")
            ):
                # "leave Dec 10 and come back two days later": relative to the NEW
                # departure, never to the stale return of the old dates.
                anchor = _parse_iso(str(new_dep))
            elif anchor is None and days > 0:
                # No return yet: "come back a week later" counts from the departure.
                anchor = _parse_iso(str(new_dep or base.get("departure_date") or ""))
        if anchor is None:
            extracted.pop(slot, None)
            continue
        extracted[slot] = (anchor + timedelta(days=days)).isoformat()
        shifted.add(slot)
    if shifted:
        duration = None
    if "return_date" in shifted:
        return_weekday = None

    merged = dict(base)
    old_departure = merged.get("departure_date")
    merged.update(extracted)

    # A new departure invalidates a stale return the user did not restate — but
    # "leave a day earlier" keeps the trip's return (the stay just grows).
    if (
        "departure_date" in extracted
        and "departure_date" not in shifted
        and "return_date" not in extracted
        and extracted["departure_date"] != old_departure
    ):
        merged.pop("return_date", None)
    if duration is not None and merged.get("departure_date") and "return_date" not in extracted:
        ret = return_from_duration(str(merged["departure_date"]), duration[0], duration[1])
        if ret:
            merged["return_date"] = ret
            extracted["return_date"] = ret
    if (
        isinstance(return_weekday, int)
        and "return_date" in extracted
        and merged.get("departure_date")
        and str(merged["return_date"]) < str(merged["departure_date"])
    ):
        # "Actually I'll come back Friday": the Friday AFTER the departure, not the
        # one after today (which falls before the trip and used to clear the return).
        dep = _parse_iso(str(merged["departure_date"]))
        if dep is not None:
            delta = (return_weekday - dep.weekday()) % 7 or 7
            merged["return_date"] = (dep + timedelta(days=delta)).isoformat()
            extracted["return_date"] = merged["return_date"]
    if same_day and "return_date" not in shifted and merged.get("departure_date"):
        # "make it a day trip" / "back the same day": an explicit same-day return.
        merged["return_date"] = merged["departure_date"]
        extracted["return_date"] = merged["return_date"]

    notice = _reject_collapsed_dates(base, merged, extracted, same_day=same_day)
    if notice is None and vague is not None:
        notice = _vague_date_notice(str(vague), merged, explicit_dates)
    if (
        merged.get("return_date")
        and merged.get("departure_date")
        and str(merged["return_date"]) < str(merged["departure_date"])
    ):
        merged.pop("return_date", None)
        extracted.pop("return_date", None)

    if source != "none":
        for slot in ("origin", "destination"):
            if slot in merged:
                merged[slot] = _normalize_location(merged[slot])
                if slot in extracted:
                    extracted[slot] = merged[slot]
        # Atlas plans trips for the signed-in traveler (NFR-SEC6): one traveler
        # and USD unless the conversation says otherwise.
        merged.setdefault("travelers", 1)
        merged.setdefault("currency", "USD")

    slots = {slot: merged.get(slot) for slot in SLOT_FIELDS}
    return ResolvedSlots(slots=slots, extracted=extracted, source=source, notice=notice)


def _vague_date_notice(
    vague: str, merged: Mapping[str, Any], explicit_dates: set[str]
) -> str | None:
    """The clarifying question for a date phrase too vague to apply (product gap 8).

    None when the same message also wrote the date(s) out: an explicit date wins.
    """
    if vague == "next week":
        if "departure_date" in explicit_dates:
            return None
        return (
            'Which day next week? "Next week" is not precise enough for me to pick a '
            'date. Say, for example, "next Tuesday" or "Nov 18".'
        )
    if explicit_dates >= _DATE_SLOTS:
        return None
    dep, ret = merged.get("departure_date"), merged.get("return_date")
    kept = f", so I kept your dates as {dep} to {ret}" if dep and ret else ""
    return (
        f'How many days exactly? "Several" is not precise enough for me to change '
        f'your dates{kept}. Say, for example, "3 days later" or "2 more nights".'
    )


def _reject_collapsed_dates(
    base: Mapping[str, Any],
    merged: dict[str, Any],
    extracted: dict[str, Any],
    *,
    same_day: bool,
) -> str | None:
    """Keep the prior dates when a follow-up would collapse a trip with nights.

    A message must never turn a trip that had nights into a backwards trip, nor
    silently into a same-day return (unless the user said "same day" / "day
    trip"). The prior return (and, if the departure moved past it, the prior
    departure) is restored and the returned question asks for the intended date.
    """
    if not any(slot in extracted for slot in _DATE_SLOTS):
        return None  # chips / prior only: an explicit edit is not second-guessed
    base_dep, base_ret = base.get("departure_date"), base.get("return_date")
    if not base_dep or not base_ret or str(base_ret) <= str(base_dep):
        return None  # the trip had no nights to lose
    new_dep, new_ret = merged.get("departure_date"), merged.get("return_date")
    if not new_dep or not new_ret:
        return None
    if str(new_ret) > str(new_dep) or (str(new_ret) == str(new_dep) and same_day):
        return None

    if str(new_ret) == str(new_dep):
        problem = f"That would put your return on {new_ret}, the same day you depart"
    else:
        problem = f"That would put your return ({new_ret}) before your departure ({new_dep})"
    merged["return_date"] = base_ret
    extracted.pop("return_date", None)
    if str(base_ret) <= str(new_dep):
        merged["departure_date"] = base_dep
        extracted.pop("departure_date", None)
    return (
        f"{problem}. I kept your dates as {merged['departure_date']} to "
        f"{merged['return_date']}. Which day would you like to come back? "
        f'(Say "same day" if you mean a day trip.)'
    )
