"""Airport / city vocabulary for the mock travel inventory.

The planner accepts free-form locations ("Chicago", "Chicago (ORD)", "NYC",
"JFK"). Flights are keyed by IATA airport code and hotels by city name, so both
sides resolve through this single table:

  resolve_airport("Chicago (ORD)") -> "ORD"
  resolve_city("JFK")              -> "New York"

Only the airports the mock inventory serves are listed; anything else resolves
to None (callers then keep the user's raw text and the search returns nothing —
the inventory never invents a route it does not know).
"""

from __future__ import annotations

import re

from config.constants.city_names import strip_us_region_suffix

# IATA code -> canonical city name (the city name hotels are filed under).
AIRPORT_CITIES: dict[str, str] = {
    "SFO": "San Francisco",
    "JFK": "New York",
    "ORD": "Chicago",
    "LHR": "London",
}

# Lower-cased aliases (city names, nicknames, airport names) -> IATA code.
_ALIASES: dict[str, str] = {
    "san francisco": "SFO",
    "sf": "SFO",
    "san francisco international": "SFO",
    "new york": "JFK",
    "new york city": "JFK",
    "nyc": "JFK",
    "manhattan": "JFK",
    "kennedy": "JFK",
    "john f kennedy": "JFK",
    "jfk airport": "JFK",
    "chicago": "ORD",
    "o'hare": "ORD",
    "ohare": "ORD",
    "chicago o'hare": "ORD",
    "chicago ohare": "ORD",
    "london": "LHR",
    "heathrow": "LHR",
    "london heathrow": "LHR",
}

_CODE_RE = re.compile(r"\b([A-Za-z]{3})\b")


def _clean(text: str) -> str:
    text = text.strip().strip(".,;:!?\"'").strip()
    return re.sub(r"\s+", " ", text)


def resolve_airport(location: str | None) -> str | None:
    """Return the IATA code for a known airport/city, else None.

    Accepts a bare code ("ord"), a city ("Chicago"), a city with a state/country
    suffix ("Chicago, IL"), or a mixed label ("Chicago (ORD)").
    """
    if not location:
        return None
    text = _clean(str(location))
    if not text:
        return None
    upper = text.upper()
    if upper in AIRPORT_CITIES:
        return upper
    lower = text.lower()
    if lower in _ALIASES:
        return _ALIASES[lower]
    # "Chicago (ORD)" / "ORD - Chicago": prefer an explicit known code.
    for match in _CODE_RE.finditer(text):
        code = match.group(1).upper()
        if code in AIRPORT_CITIES and match.group(1).isupper():
            return code
    # "Chicago, IL" / "London, UK" / "New York (any airport)"
    head = _clean(re.split(r"[,(/]", text, maxsplit=1)[0])
    if head.lower() in _ALIASES:
        return _ALIASES[head.lower()]
    if head.upper() in AIRPORT_CITIES and head.isupper():
        return head.upper()
    # "New York NY" / "NEW YORK NY 10017" / "Chicago, IL 60601": the city without a
    # trailing US state code / ZIP (shared rule: config.constants.city_names).  Exact
    # alias lookup only, so "Manhattan Beach CA" never resolves to Manhattan.
    stripped = strip_us_region_suffix(head).lower()
    if stripped != head.lower() and stripped in _ALIASES:
        return _ALIASES[stripped]
    return None


def resolve_city(location: str | None) -> str | None:
    """Return the canonical city name for a known airport/city, else None."""
    code = resolve_airport(location)
    return AIRPORT_CITIES[code] if code else None


def known_location_names() -> list[str]:
    """All alias names + codes, longest first (for free-text scanning)."""
    names = set(_ALIASES) | {c.lower() for c in AIRPORT_CITIES}
    return sorted(names, key=len, reverse=True)
