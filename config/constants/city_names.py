"""City-name normalisation shared by every place that matches a location to a city.

Receipts, OCR and people write the same place many ways: "New York, NY",
"New York NY", "NEW YORK NY 10017", "New York, NY 10017-1234".  Every consumer
(HOTEL-CAP / MEALS-PD city tiers in ``PolicyEvalTool``, the receipt-vs-segment
city comparison in pre-submit, the corpus ``city_tier`` helper, the planner's
location resolver and the approval split-trip destination key) derives its
comparable keys HERE so one format can never resolve to Tier 1 in one place and
Tier 3 in another.

Matching stays exact: these helpers only produce candidate keys; a key matches a
listed city / alias only when it is EQUAL to it (never a substring), so
"Manhattan Beach CA" -> "manhattan beach" never becomes "manhattan" (NYC).

Only a trailing US ZIP and/or a trailing US state / territory postal code is
stripped, and only when something remains.  A two-letter last token that is not a
US code ("Paris FR", "London UK") is kept as-is.  "DC" is deliberately NOT
strippable: it is part of the city's own name ("Washington DC" is listed as such
and must not collapse to "Washington").

A trailing US country token ("US", "USA", "U.S.", "U.S.A.", "United States") is
removed first, but ONLY when it ends the text and directly follows a US state /
territory code, "DC" or a ZIP that itself follows a city: "New York NY USA" and
"NEW YORK NY 10017 US" -> "New York".  "New York USA", "NY USA" and
"Paris FR USA" are kept as written.
"""

from __future__ import annotations

import re

# USPS state + territory codes (lower-case).  "dc" is intentionally absent (see above).
US_REGION_CODES: frozenset[str] = frozenset(
    {
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
        "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
        "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
        "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
        "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
        # territories
        "pr", "gu", "vi", "as", "mp",
    }
)  # fmt: skip

_ZIP5 = re.compile(r"^\d{5}$")
_ZIP4 = re.compile(r"^\d{4}$")
_ZIP9 = re.compile(r"^\d{5}-\d{4}$")

# Trailing US country tokens (casefolded, dots removed): "US", "USA", "U.S.", "U.S.A.".
_US_COUNTRY_TOKENS: frozenset[str] = frozenset({"us", "usa"})
# Codes that may precede a country token: the states / territories plus "dc"
# (which is never stripped itself, see the module docstring).
_COUNTRY_PRECEDING_CODES: frozenset[str] = US_REGION_CODES | {"dc"}


def _tok(token: str) -> str:
    return token.casefold().replace(".", "")


def _strip_zip(tokens: list[str]) -> list[str]:
    """*tokens* without a trailing ZIP (5, 5+4 or "5 4"), keeping at least one token."""
    if len(tokens) >= 2 and _ZIP9.match(tokens[-1]):
        return tokens[:-1]
    if len(tokens) >= 3 and _ZIP4.match(tokens[-1]) and _ZIP5.match(tokens[-2]):
        return tokens[:-2]  # "10017 1234" after punctuation was normalised away
    if len(tokens) >= 2 and _ZIP5.match(tokens[-1]):
        return tokens[:-1]
    return tokens


def _strip_us_country(tokens: list[str]) -> list[str]:
    """*tokens* without a trailing US country token, only when that token directly
    follows a ZIP or a US state / territory / DC code which itself follows a city."""
    if tokens and _tok(tokens[-1]) in _US_COUNTRY_TOKENS:
        rest = tokens[:-1]
    elif len(tokens) >= 2 and [_tok(t) for t in tokens[-2:]] == ["united", "states"]:
        rest = tokens[:-2]
    else:
        return tokens
    if len(_strip_zip(rest)) < len(rest):  # "... 10017 USA": a ZIP after a city
        return rest
    if len(rest) >= 2 and _tok(rest[-1]) in _COUNTRY_PRECEDING_CODES:
        return rest  # "New York NY USA" / "Washington DC USA"
    return tokens


def norm_city(name: str) -> str:
    """Casefold, drop dots ('D.C.' -> 'dc'), collapse punctuation / whitespace."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in name.casefold().replace(".", ""))
    return " ".join(cleaned.split())


def strip_us_region_suffix(name: str) -> str:
    """*name* without a trailing US country token, US ZIP and / or US state /
    territory code.

    Works on whitespace tokens and keeps the remaining text as written:
    "New York NY 10017" -> "New York", "chicago o'hare il" -> "chicago o'hare",
    "NEW YORK NY 10017 US" -> "NEW YORK".  Returns *name* unchanged when there is
    nothing to strip or nothing would remain ("NY" stays "NY"; "Paris FR" stays
    "Paris FR"; "New York USA" stays as written - no state / ZIP before the country).
    """
    tokens = _strip_zip(_strip_us_country(name.split()))
    if len(tokens) >= 2 and _tok(tokens[-1]) in US_REGION_CODES:
        tokens = tokens[:-1]
    return " ".join(tokens) if tokens else name


def city_keys(location: str) -> set[str]:
    """Comparable (normalised) keys of a location.

    * its city part ('New York, NY' -> 'new york');
    * its first two parts ('Washington, DC, USA' -> 'washington dc');
    * the whole;
    * each of those with a trailing US country / ZIP / state code removed
      ('New York NY' -> 'new york', 'NEW YORK NY 10017 US' -> 'new york').
    """
    parts = location.split(",")
    base = {norm_city(parts[0]), norm_city(",".join(parts[:2])), norm_city(location)}
    keys = set(base)
    for key in base:
        keys.add(strip_us_region_suffix(key))
    keys.discard("")
    return keys


def primary_city_key(location: str) -> str:
    """The single key naming the city itself: the part before the first comma,
    normalised, without a trailing US ZIP / state code.  'New York, NY',
    'New York NY' and 'NEW YORK NY 10017' all give 'new york'."""
    return strip_us_region_suffix(norm_city(location.split(",")[0]))


def same_city(a: str, b: str) -> bool:
    """Whether two locations name the same city ('New York NY' ~ 'New York, NY')."""
    key = primary_city_key(a)
    return bool(key) and key == primary_city_key(b)
