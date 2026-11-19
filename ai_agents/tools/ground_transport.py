"""Deterministic ground-transport sub-type classifier (product gap 8).

Atlas files taxi, rideshare, car rentals, rail, transit and parking under ONE expense
category (``ground_transport``).  Policy clauses, however, treat them differently
(GRND-01 rail, GRND-02 rideshare/taxi, GRND-03 parking, GRND-04 transit, CAR-01..05
rentals), and a finance admin who authors "rideshare cap $50" must not cap train
tickets and airport parking too.

``classify_ground_transport`` reads the entry's own words — vendor, description,
card merchant — plus the linked trip segment type and returns ONE sub-type:

  * ``rideshare_taxi`` / ``car_rental`` / ``rail`` / ``transit`` / ``parking`` when
    exactly one family is named;
  * ``ground_transport`` (the BROAD category) when nothing is recognised OR when the
    words name more than one family ("Uber to Penn Station train") — ambiguous lines
    FAIL SAFE to the broad category; never to a guess.

Pure, deterministic, LLM-free.  Matching is on whole words / phrases (never
substrings: "Cabernet" is not a "cab", "Avis" inside "Aviso" is not a rental).
Longer phrases are matched first and consumed, so "light rail" is transit (not rail)
and "Enterprise Rent-A-Car" is one rental match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BROAD = "ground_transport"
RIDESHARE_TAXI = "rideshare_taxi"
CAR_RENTAL = "car_rental"
RAIL = "rail"
TRANSIT = "transit"
PARKING = "parking"

GROUND_SUBTYPES: tuple[str, ...] = (RIDESHARE_TAXI, CAR_RENTAL, RAIL, TRANSIT, PARKING)

# Human-readable names (rule summaries, flag reasons).
SUBTYPE_LABELS: dict[str, str] = {
    RIDESHARE_TAXI: "rideshare/taxi",
    CAR_RENTAL: "car rental",
    RAIL: "rail",
    TRANSIT: "public transit",
    PARKING: "parking",
    BROAD: "ground transport",
}

# Phrases per family.  Every entry is matched as a whole word / phrase, case-insensitive,
# with any run of spaces / hyphens / dots between its words.
_PHRASES: dict[str, tuple[str, ...]] = {
    RIDESHARE_TAXI: (
        "uber",
        "uberx",
        "uber black",
        "lyft",
        "taxi",
        "taxis",
        "taxicab",
        "cab",
        "cabs",
        "yellow cab",
        "rideshare",
        "ride share",
        "ride hail",
        "ridehail",
        "car service",
        "black car",
        "limo",
        "limousine",
        "chauffeur",
        "curb",
        "didi",
        "free now",
        "gett",
    ),
    CAR_RENTAL: (
        "hertz",
        "avis",
        "enterprise",
        "enterprise rent a car",
        "national car rental",
        "alamo",
        "sixt",
        "thrifty",
        "dollar rent a car",
        "budget rent a car",
        "budget car rental",
        "europcar",
        "zipcar",
        "car rental",
        "rental car",
        "rent a car",
        "car hire",
    ),
    RAIL: (
        "amtrak",
        "acela",
        "eurostar",
        "via rail",
        "sncf",
        "tgv",
        "deutsche bahn",
        "db bahn",
        "ice train",
        "trenitalia",
        "italo",
        "renfe",
        "shinkansen",
        "jr east",
        "jr central",
        "national rail",
        "avanti west coast",
        "lner",
        "train",
        "trains",
        "rail",
        "railway",
        "railways",
        "intercity",
    ),
    TRANSIT: (
        "public transit",
        "transit",
        "metro",
        "subway",
        "underground",
        "tube",
        "bus",
        "buses",
        "light rail",
        "tram",
        "streetcar",
        "mta",
        "metrocard",
        "omny",
        "bart",
        "clipper",
        "septa",
        "wmata",
        "smartrip",
        "oyster",
        "tfl",
        "muni",
        "caltrain",
        "lirr",
        "metro north",
        "nj transit",
        "path train",
        "airtrain",
        "ferry",
    ),
    PARKING: (
        "parking",
        "car park",
        "park and fly",
        "park n fly",
        "park ride fly",
        "the parking spot",
        "spothero",
        "parkwhiz",
        "valet",
        "garage",
    ),
}

# Built once: (pattern, family), longest phrase first so it is consumed before its parts.
_SEP = r"[\s\-./&']+"


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in phrase.split()]
    return re.compile(r"(?<![0-9a-z])" + _SEP.join(words) + r"(?![0-9a-z])", re.IGNORECASE)


_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (_phrase_pattern(phrase), family, phrase)
    for phrase, family in sorted(
        ((p, fam) for fam, phrases in _PHRASES.items() for p in phrases),
        key=lambda pf: (-len(pf[0]), pf[0]),
    )
)

# Trip segment types that name the family outright.
_SEGMENT_FAMILY: dict[str, str] = {"rail": RAIL, "car": CAR_RENTAL}


@dataclass(frozen=True)
class GroundClassification:
    """Result of classifying one ground-transport line.

    ``subtype`` is one of ``GROUND_SUBTYPES`` or ``BROAD``.  ``families`` lists every
    family the evidence named (more than one -> ambiguous -> BROAD); ``matched`` the
    phrases that matched (for reasons / audit, never used to decide on its own).
    """

    subtype: str
    families: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return len(self.families) > 1

    @property
    def classified(self) -> bool:
        return self.subtype in GROUND_SUBTYPES


def _families_in(text: str) -> tuple[set[str], list[str]]:
    families: set[str] = set()
    matched: list[str] = []
    remaining = text
    for pattern, family, phrase in _PATTERNS:
        if pattern.search(remaining):
            families.add(family)
            matched.append(phrase)
            remaining = pattern.sub(" ", remaining)
    return families, matched


def classify_ground_transport(
    *texts: str | None, segment_type: str | None = None
) -> GroundClassification:
    """Classify a ground-transport line from its words and its trip segment type.

    ``texts`` are the vendor, description, card merchant, OCR vendor ... (None / blank
    ignored).  ``segment_type`` is the linked trip segment's type ("rail" / "car"),
    which names a family as strongly as a word does.  Exactly one family -> that
    sub-type; none or several -> ``BROAD`` (fail safe).
    """
    text = " | ".join(t.strip() for t in texts if isinstance(t, str) and t.strip())
    families, matched = _families_in(text) if text else (set(), [])
    seg_family = _SEGMENT_FAMILY.get(str(segment_type or "").strip().lower())
    if seg_family is not None:
        families.add(seg_family)
        matched.append(f"{segment_type} segment")
    ordered = tuple(f for f in GROUND_SUBTYPES if f in families)
    subtype = ordered[0] if len(ordered) == 1 else BROAD
    return GroundClassification(subtype=subtype, families=ordered, matched=tuple(matched))


@dataclass(frozen=True)
class TrustedClassification:
    """Sub-type of a ground line with the trust of the evidence behind it.

    ``families``: every family named by the claimant's words, the trusted words or the
    trusted segment (the CANDIDATES).  ``verified``: trusted evidence names exactly one
    family and the claimant's words name no other.  ``subtype``: that family when
    verified; else the single candidate when only one family is named at all (typed-only
    lines keep their wording, but stay unverified); else ``BROAD``.
    """

    subtype: str
    families: tuple[str, ...]
    verified: bool
    # Known limits (ground): the families NAMED by any word (typed or trusted) or the
    # segment, BEFORE an unconfirmed line is widened to every sub-type.  The worst-case
    # check (``ground_worst_case``) keeps a kind rule at risk when its sub-type is named
    # here: words may only ADD scrutiny, never remove it.
    named: tuple[str, ...] = ()


def classify_with_trust(
    typed: tuple[str | None, ...] | list[str | None],
    trusted: tuple[str | None, ...] | list[str | None],
    *,
    segment_type: str | None = None,
    evidence_destroyed: bool = False,
) -> TrustedClassification:
    """Classify a ground line WITHOUT letting the claimant's words decide it alone
    (security review): typed words that disagree with the receipt / card / segment make
    every named family a candidate, and words with no trusted evidence never verify.

    ``evidence_destroyed``: the receipt's own vendor reading was overwritten by the
    claimant and no OCR snapshot of it is left — nothing can verify the line.
    """
    typed_cls = classify_ground_transport(*typed)
    # Integration (security review): the trip segment's type is client-writable
    # (POST /trips), so it never verifies a line on its own — the RECEIPT's OCR words or
    # the card merchant must name the family; a segment only corroborates (a segment of
    # another family makes the line ambiguous, never verified).
    text_fams = set(classify_ground_transport(*trusted).families)
    seg_fams = set(classify_ground_transport(segment_type=segment_type).families)
    t_fams, w_fams = text_fams | seg_fams, set(typed_cls.families)
    families = tuple(f for f in GROUND_SUBTYPES if f in t_fams | w_fams)
    named = families
    verified = (
        not evidence_destroyed
        and len(text_fams) == 1
        and w_fams <= text_fams
        and seg_fams <= text_fams
    )
    if verified:
        subtype = next(iter(t_fams))
    elif len(families) == 1:
        subtype = families[0]
    else:
        subtype = BROAD
    if not verified and (evidence_destroyed or not text_fams):
        # Review round 5: no trusted evidence names a family (typed words only, a vendor
        # that names nothing, or a receipt vendor the claimant overwrote) -> the line is
        # UNCONFIRMED: it may be any kind, so every sub-type is a candidate and every
        # kind rule reaches it (could-not-verify -> approver check), never skipped.
        families = GROUND_SUBTYPES
    return TrustedClassification(subtype=subtype, families=families, verified=verified, named=named)


def unconfirmed(families: tuple[str, ...] | list[str] | set[str], verified: bool) -> bool:
    """An unverified line whose candidates are every sub-type (``classify_with_trust``)."""
    return not verified and set(GROUND_SUBTYPES) <= set(families)


def subtype_label(subtype: str | None) -> str:
    return SUBTYPE_LABELS.get(str(subtype or BROAD), "ground transport")


# ---------------------------------------------------------------------------
# Small extractors used to build the evaluation item (all deterministic)
# ---------------------------------------------------------------------------

_PREMIUM_RAIL = re.compile(
    r"(?<![0-9a-z])(first|1st|business|premier|premium|club)(?:[\s\-]+(class|cl))?(?![0-9a-z])",
    re.IGNORECASE,
)
_STANDARD_RAIL = re.compile(
    r"(?<![0-9a-z])(standard|economy|coach|second|2nd|value|saver|regular)"
    r"(?:[\s\-]+(class|cl|fare|seat))?(?![0-9a-z])",
    re.IGNORECASE,
)


def rail_class(*texts: str | None) -> str | None:
    """'first' / 'business' / 'standard' named in the words, else None.

    Both a premium and a standard class named (e.g. "standard upgrade to first") is
    ambiguous -> None (the evaluator then reports it could not verify the class).
    """
    text = " ".join(t for t in texts if isinstance(t, str) and t)
    if not text:
        return None
    premium = {m.group(1).lower() for m in _PREMIUM_RAIL.finditer(text)}
    standard = bool(_STANDARD_RAIL.search(text))
    if premium and standard:
        return None
    if premium:
        if premium & {"first", "1st"}:
            return "first"
        return "business"
    return "standard" if standard else None


def premium_rail_class(*texts: str | None) -> str | None:
    """'first' / 'business' when ANY of the words names a premium class, even beside a
    standard one ("Coach upgrade to First Class") — the reading used AGAINST a claim."""
    text = " ".join(t for t in texts if isinstance(t, str) and t)
    premium = {m.group(1).lower() for m in _PREMIUM_RAIL.finditer(text)}
    if not premium:
        return None
    return "first" if premium & {"first", "1st"} else "business"


_HOURS_MINUTES = re.compile(
    r"(?<![0-9.])(\d{1,2})\s*(?:h|hr|hrs|hours?)\s*(\d{1,2})\s*(?:m|min|mins|minutes?)(?![a-z])",
    re.IGNORECASE,
)
_HOURS = re.compile(
    r"(?<![0-9.])(\d{1,2}(?:\.\d{1,2})?)\s*-?\s*(?:h|hr|hrs|hours?)(?![a-z])", re.IGNORECASE
)


def stated_hours(*texts: str | None) -> float | None:
    """A single journey duration stated in the words ("2h30m", "3.5 hours", "3-hour").

    None when no duration or two different durations are stated (ambiguous).
    """
    values: set[float] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        rest = text
        for m in _HOURS_MINUTES.finditer(text):
            values.add(round(int(m.group(1)) + int(m.group(2)) / 60, 2))
        rest = _HOURS_MINUTES.sub(" ", rest)
        for m in _HOURS.finditer(rest):
            values.add(round(float(m.group(1)), 2))
    values = {v for v in values if 0 < v <= 48}
    return values.pop() if len(values) == 1 else None


_TIP_WORDS = re.compile(r"(?<![0-9a-z])(tip|tips|gratuity|gratuities)(?![0-9a-z])", re.IGNORECASE)


def mentions_tip(*texts: str | None) -> bool:
    return any(isinstance(t, str) and _TIP_WORDS.search(t) for t in texts)


def is_tip_line(description: str | None) -> bool:
    return isinstance(description, str) and bool(_TIP_WORDS.search(description))


_PREMIUM_LOT = re.compile(
    r"(?<![0-9a-z])(valet|premium|short[\s\-]*term|hourly|terminal garage|covered premium)"
    r"(?![0-9a-z])",
    re.IGNORECASE,
)


def premium_parking(*texts: str | None) -> str | None:
    """The premium-lot word named in the text (GRND-03 reimburses the economy lot)."""
    for t in texts:
        if isinstance(t, str):
            m = _PREMIUM_LOT.search(t)
            if m:
                return m.group(1).lower()
    return None


# GRND-03 covers AIRPORT parking only.  A parking line is airport parking when its words
# say so ("airport", an airport park-and-fly operator) or name a major airport by its
# IATA code, or when it is linked to a flight segment (``ground_fields``).  Hotel valet,
# a city garage, a street meter ... are parking but NOT airport parking: GRND-03 never
# reaches them.
_AIRPORT_WORDS = re.compile(
    r"(?<![0-9a-z])(airport|airports|aeropuerto|aeroport|park[\s\-]*(?:and|n|&|'n'?|n')[\s\-]*fly"
    r"|park[\s\-]*ride[\s\-]*fly|park[\s\-]*sleep[\s\-]*fly|the[\s\-]+parking[\s\-]+spot"
    r"|wallypark|fast[\s\-]*park|terminal[\s\-]+(?:parking|garage|lot))(?![0-9a-z])",
    re.IGNORECASE,
)
# Major-airport IATA codes, matched CASE-SENSITIVELY as whole words ("SFO Long Term").
# Codes that are also common upper-case words / city prefixes on OCR text (SAN, LAS,
# MIA, AUS, DAL, IND, HOU, SEA, PIT, OAK, MAD, DUB, SIN ...) are deliberately left out:
# a false "airport" would put a city garage under GRND-03.
_AIRPORT_CODES = frozenset(
    "ATL LAX ORD DFW DEN JFK SFO EWR CLT PHX IAH MCO BOS MSP FLL DTW PHL LGA BWI SLC "
    "IAD DCA MDW TPA PDX HNL BNA RDU SJC SMF SNA MCI CLE CVG CMH LHR LGW CDG FRA AMS "
    "FCO MUC ZRH YYZ YVR NRT HND HKG DXB".split()
)
_UPPER_WORD = re.compile(r"(?<![0-9A-Za-z])([A-Z]{3})(?![0-9A-Za-z])")


def airport_named(*texts: str | None) -> str | None:
    """The word / airport code that says the line is at an airport, else None."""
    for t in texts:
        if not isinstance(t, str):
            continue
        m = _AIRPORT_WORDS.search(t)
        if m:
            return m.group(1).lower()
        for code in _UPPER_WORD.findall(t):
            if code in _AIRPORT_CODES:
                return code
    return None
