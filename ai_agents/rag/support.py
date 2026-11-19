"""Deterministic, offline sentence-support check for cited policy answers (C7-06 / C7-10 residual).

Why this exists
---------------
R5-T3 scored a cited sentence by raw token overlap (lower-cased ``[a-z0-9]+`` sets, no stop-word
removal, no stemming) against its cited leaf clause.  Faithful paraphrases of terse policy
clauses lost points on function words ("the", "for", "yes") and inflection ("cap" vs "caps",
"cities" vs "city") and scored 0.30-0.50, so 8/14 clear in-corpus staging questions were
withheld; passing drafts sat at 0.60-0.62, a razor-thin margin above the 0.55 floor that LLM
phrasing variance (temperature 0.3) flipped run to run.

What it does (per answer sentence, against the clause text the sentence CITES)
------------------------------------------------------------------------------
Modelled on claim-level attribution checks (ALCE citation recall: "do the cited passages support
this sentence?"; RAGAS faithfulness: share of a statement's content supported by the context),
implemented as a cheap lexical NLI proxy plus hard guards for the error classes a lexical
score cannot see:

1. **Content-word recall** — lower-case, strip the ``[Label §ref]`` markers, drop stop words and
   answer scaffolding ("yes", "should", "book", ...), fold inflection/derivation with the same
   conservative stemmer the C7-10 lexical rescue uses (plus -able/-ment/-ation/-al/-er and
   consonant un-doubling), map a small closed set of T&E synonyms to one concept (hotel ~
   lodging ~ room, cap ~ limit, wine ~ alcohol, ...).  Score = share of the sentence's content
   concepts present in the cited evidence.  Concepts the sentence merely ECHOES from the user's
   question (e.g. "domestic", "Chicago") are premise, not a policy claim: they count when
   supported and are left out of the denominator when not — for an ANCHORED sentence only (5).
2. **Numeric guard (hard)** — every number / amount / percentage in the sentence ($300, 300.00,
   1,000, 20%, "twelve") must appear in the cited evidence; otherwise the sentence fails
   regardless of score.  The only exception is a narrowly-accepted PREMISE figure from the
   question: no comparator in the sentence ("a 12-hour flight", not "flights over 12 hours"),
   consistent with the cited threshold it is applied to (20% vs "> 15%"), and — for money and
   percentages — compared against a cited figure or threshold.  A wrong amount the user typed
   ("Is the cap $500?") is never confirmed.
3. **Direction guard (hard)** — a cited figure keeps the clause's comparator ("under 10 hours"
   vs "> 10 hours"; "> 15% auto-approved" vs "≤ 15% are auto-approved").  A comparator after
   the figure counts too ("15% or more", "3 hours or less", "6 hours and up").
4. **Polarity guard (hard)** — the sentence's negation must match the polarity of the evidence
   segment it overlaps most ("First class is reimbursable" vs "First class is never
   reimbursable" fails); a POSITIVE sentence tied with a negated segment fails, and so does a
   positive sentence naming a subject only a negated segment mentions ("Wi-Fi and minibar are
   reimbursable" against LOD-04's "Minibar ... are not").  A leading
   "Yes," / "No," answers the QUESTION and is ignored; a parenthetical "(if not in per diem)"
   does not negate the statement around it.
5. **Anchor guard (hard)** — the sentence must state something the cited clause backs beyond
   the user's own words (a supported non-question concept or a cited figure), unless the clause
   covers the whole restatement.  "Yes, you can expense your gym membership while traveling
   [§MEAL-01]" is pure echo and fails.
6. **Contrast guard (hard)** — a claim concept the cited clause lacks but a DIFFERENT retrieved
   clause contains marks a mis-attribution: "book Business class for a 3-hour flight
   [§AIR-01]" borrows "business" from AIR-03.  A concept echoed from the question is a hit
   when that sibling matches the sentence at least as well as the cited clause.

Everything is pure Python + ``re`` — no model, no network, no new dependency — so it runs on the
answer path at zero latency/cost and is byte-deterministic (no set-order leaks into output).

Known limit (documented, by design): a lexical check cannot judge semantic relations such as
"Chicago is Tier 1" when the cited clause lists tiers without cities; the LLM critic remains the
semantic gate and is still REQUIRED for any text to be shown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from ai_agents.rag.lexical import _STOP_WORDS as _LEXICAL_STOP_WORDS
from ai_agents.rag.lexical import _stem as _lexical_stem

# ---------------------------------------------------------------------------- vocabulary

# Clause-level negation cues (kept OUT of the recall stop words so polarity can be read).
# The prefix "non-" is deliberately absent: "non-Tier-1 cities" negates a noun modifier, not the
# statement ("Domestic US cities (non-Tier-1): USD 180 per night" is a positive rule).
_NEGATION_CUES: frozenset[str] = frozenset(
    {"not", "no", "never", "cannot", "nor", "neither", "none"}
)

# Words that qualify / restrict a statement.  A DROPPED sentence carrying one of these may be a
# caveat the remaining sentences depend on ("... This cap does not apply to Tier-1 cities."),
# so the caller withholds the whole draft rather than show it without the caveat.
QUALIFIER_CUES: frozenset[str] = _NEGATION_CUES | frozenset(
    {
        "except",
        "unless",
        "however",
        "but",
        "only",
        "excluding",
        "exclude",
        "excludes",
        "exception",
        "exceptions",
        "provided",
        "if",
        "although",
        "though",
        "instead",
    }
)

# Requirement / condition words.  A dropped sentence that states a precondition ("You must get
# VP sign-off first", "> 15% needs manager approval with justification") is a caveat just like
# a negation: showing the remaining "Yes, business class is permitted ..." without it would
# turn a conditional permission into an unconditional one.  Matched on folded concepts, so
# inflections ("requires", "approved", "needs") and synonyms ("authorization") are covered.
_CONDITION_WORDS: tuple[str, ...] = (
    "must",
    "need",
    "require",
    "approval",
    "justification",
    "justify",
    "documentation",
    "document",
    "receipt",
    "sign",
    "signoff",
    "permission",
    "when",
    "before",
    "after",
    "until",
    "prior",
    "subject",
    "condition",
    "conditional",
)

# Recall stop words: the retrieval module's closed-class list MINUS negations (handled by the
# polarity guard) PLUS answer scaffolding that carries no policy claim of its own.
_SUPPORT_STOP_WORDS: frozenset[str] = (_LEXICAL_STOP_WORDS - _NEGATION_CUES) | frozenset(
    {
        # answer scaffolding / discourse
        "yes",
        "yeah",
        "ok",
        "okay",
        "please",
        "note",
        "generally",
        "typically",
        "usually",
        "also",
        "only",
        "however",
        "therefore",
        "thus",
        "since",
        "because",
        "while",
        "just",
        "even",
        "still",
        "here",
        "like",
        "including",
        "include",
        "includes",
        "etc",
        "able",
        # generic referents the leaf clause rarely repeats
        "company",
        "policy",
        "policies",
        "employee",
        "employees",
        "traveler",
        "travelers",
        "traveller",
        "travellers",
        "staff",
        "each",
        "every",
        "other",
        "such",
        "both",
        "either",
        # action verbs implied by any rule ("you should BOOK economy")
        "book",
        "booking",
        "choose",
        "select",
        "use",
        "using",
        "take",
        "taking",
        "opt",
        "make",
        "go",
        "going",
        "want",
        "apply",
        "applies",
        "case",
        "within",
        "via",
        "will",
        "shall",
        # unit glue: "$300 per night" — "diem" carries "per diem"; numbers are guarded apart
        "per",
        # comparison words: clauses use symbols (<, >, ≤); the numeric guard checks the figure
        "under",
        "over",
        "above",
        "below",
        "less",
        "greater",
        "least",
        "most",
        "up",
        # contraction residue
        "s",
        "t",
        "ll",
        "d",
        "re",
        "ve",
        "m",
    }
)

# Closed synonym classes (T&E vocabulary).  Each raw word is folded, then mapped to the class
# representative so a paraphrase and the clause meet.  Directional generalisations only
# (wine -> alcohol is entailed; the reverse is not added as a separate word).
_SYNONYM_CLASSES: tuple[tuple[str, ...], ...] = (
    ("lodging", "hotel", "hotels", "accommodation", "accommodations", "room", "rooms", "stay"),
    ("cap", "caps", "limit", "limits", "maximum", "max", "ceiling", "capped"),
    ("flight", "flights", "fly", "flying", "air", "airfare"),
    ("meal", "meals", "food", "dining", "dinner", "lunch", "breakfast"),
    ("alcohol", "alcoholic", "wine", "beer", "liquor", "drink", "drinks", "spirits"),
    (
        "reimburse",
        "reimbursable",
        "reimbursed",
        "reimbursement",
        "expense",
        "expensed",
        "expensable",
        "covered",
        "cover",
        "claimable",
        "claim",
        "refund",
        "refunded",
    ),
    ("permit", "permitted", "allow", "allowed", "eligible", "authorized", "authorised"),
    ("require", "required", "requirement", "requires", "mandatory", "necessary", "need"),
    ("approval", "approve", "approved", "authorization", "authorisation"),
    ("day", "days", "daily"),
    ("night", "nights", "nightly"),
    ("mile", "miles", "mileage"),
    ("economy", "coach"),
    ("trip", "trips", "travel", "traveling", "travelling", "journey", "journeys"),
    ("hour", "hours", "hr", "hrs", "h"),
    ("tip", "tips", "gratuity", "gratuities"),
    ("taxi", "taxis", "cab", "cabs", "rideshare", "ride", "rides"),
    ("submit", "submitted", "submission", "file", "filed"),
    ("international", "abroad", "overseas", "foreign"),
    ("individual", "alone", "solo"),
    ("exceed", "exceeding", "exceeds", "exceeded", "exceedance"),
    ("rate", "rates", "price", "cost"),
    ("fee", "fees", "charge", "charges", "surcharge", "surcharges"),
)

# Spelled-out numbers converted to digits on BOTH sides (so "ten hours" == "10 hours").
# "one" is excluded: it is far more often a pronoun ("no one", "one of") than a quantity.
_NUMBER_WORDS: dict[str, int] = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}

# --------------------------------------------------------------------------- normalisation

# Same marker grammar as grounding._MARKER_RE (duplicated to keep this module import-cycle-free).
_MARKER_RE = re.compile(
    r"\[\s*[A-Za-z0-9&/\- ]+?\s*§\s*[A-Z]{2,}-[A-Z0-9]+"
    r"(?:\s*[,;]\s*(?:[A-Za-z0-9&/\- ]+?\s*)?§\s*[A-Z]{2,}-[A-Z0-9]+)*\s*\]"
)
# A bare clause id in prose or at the start of a leaf ("MEAL-01 Per diem ...") is an identifier,
# not a quantity — removed before number extraction so "01" never becomes the number 1.
_CLAUSE_ID_RE = re.compile(r"\b[A-Z]{2,}-[A-Z0-9]+\b")
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?(?P<pct>\s*(?:%|percent\b))?",
    re.IGNORECASE,
)
_NUMBER_WORD_RE = re.compile(
    r"\b(?P<word>" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b"
    r"(?P<pct>\s*(?:%|percent\b))?",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_LEADING_ANSWER_RE = re.compile(r"^\s*(?:yes|no)\b[\s,.;:!-]*", re.IGNORECASE)
_SEGMENT_SPLIT_RE = re.compile(r"[.;!?]\s+|[.;!?]$|\s[—–]\s")


def _fold(token: str) -> str:
    """Inflection + light derivation folding, symmetric on both sides (deterministic)."""
    if token.isdigit():
        return token
    stem = _lexical_stem(token)
    for suffix in ("ement", "ment", "ation", "abl", "ibl", "al", "er"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 4:
            stem = stem[: -len(suffix)]
            break
    if stem.endswith("e") and len(stem) >= 4:
        stem = stem[:-1]
    # un-double a final consonant pair left by -ed/-ing stripping ("permitted" -> "permit")
    if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
        stem = stem[:-1]
    return stem


def _build_synonyms() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group in _SYNONYM_CLASSES:
        head = _fold(group[0])
        for word in group:
            mapping[_fold(word)] = head
    return mapping


_SYNONYMS: dict[str, str] = _build_synonyms()
_STOP_FOLDED: frozenset[str] = frozenset(_fold(w) for w in _SUPPORT_STOP_WORDS)
_UNIT_CONCEPTS: frozenset[str] = frozenset(
    _SYNONYMS.get(_fold(w), _fold(w))
    for w in ("hour", "day", "night", "mile", "week", "month", "year", "minute")
)
_CONDITION_CONCEPTS: frozenset[str] = frozenset(
    _SYNONYMS.get(_fold(w), _fold(w)) for w in _CONDITION_WORDS
)


def _prepare(text: str) -> str:
    """Strip markers / clause ids, expand n't, split digit-letter joins ("12h" -> "12 h")."""
    text = _MARKER_RE.sub(" ", text)
    text = _CLAUSE_ID_RE.sub(" ", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\bcan't\b", "can not", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwon't\b", "will not", text, flags=re.IGNORECASE)
    text = re.sub(r"n't\b", " not", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    return text


def _canon_number(int_part: str, frac: str | None) -> str:
    raw = int_part.replace(",", "") + (f".{frac}" if frac else "")
    try:
        value = Decimal(raw).normalize()
    except InvalidOperation:  # pragma: no cover — regex guarantees digits
        return raw
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def numbers_in(text: str) -> frozenset[str]:
    """Canonical numeric facts in *text*: "300", "300.00", "$300" -> "300"; "20%" -> "20%".

    Spelled-out numbers ("twelve") map to digits.  Percent-ness is part of the identity so a
    "20%" claim is not satisfied by an unrelated "$20".
    """
    prepared = _prepare(text)
    out: set[str] = set()
    for m in _NUMBER_RE.finditer(prepared):
        value = _canon_number(m.group("int"), m.group("frac"))
        out.add(f"{value}%" if m.group("pct") else value)
    for m in _NUMBER_WORD_RE.finditer(prepared):
        value = str(_NUMBER_WORDS[m.group("word").lower()])
        out.add(f"{value}%" if m.group("pct") else value)
    return frozenset(out)


# Comparator immediately before a figure ("under 6 hours", "> 10 hours", "by more than 15%",
# "up to $300").  Alternation is matched as a SUFFIX of the preceding text, so the leftmost
# (longest) phrase wins: "no more than" is read as "<", never as "more than".
_CURRENCY_PREFIX = r"(?:\$|us\$|usd|eur|gbp|€|£)?"
_LT_RE = re.compile(
    r"(?:<=?|≤|\bunder|\bbelow|\bless\s+than|\bfewer\s+than|\bup\s+to|\bat\s+most"
    r"|\bno\s+more\s+than|\bnot\s+more\s+than|\bwithin|\bshorter\s+than|\bmaximum\s+of)"
    rf"\s*{_CURRENCY_PREFIX}\s*$",
    re.IGNORECASE,
)
_GT_RE = re.compile(
    r"(?:>=?|≥|\bover|\babove|(?<!no )(?<!not )\bmore\s+than|\bgreater\s+than|\bbeyond"
    r"|\bat\s+least|\bno\s+less\s+than|\blonger\s+than|\bminimum\s+of|\bexceed(?:s|ing|ed)?)"
    rf"\s*{_CURRENCY_PREFIX}\s*$",
    re.IGNORECASE,
)
_MONEY_BEFORE_RE = re.compile(rf"{_CURRENCY_PREFIX[:-1]}\s*$", re.IGNORECASE)
_MONEY_AFTER_RE = re.compile(r"^\s*(?:dollars?|usd|eur|euros?|gbp|pounds?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class NumberFact:
    """One figure in a text: canonical value, kind (money | pct | qty) and comparator."""

    value: str  # canonical, as in ``numbers_in`` ("300", "20%")
    kind: str  # "money" | "pct" | "qty"
    direction: str | None  # "<" | ">" | None (no comparator before or after the figure)
    unit: str | None = None  # time / distance unit concept right after it ("hour", "day", ...)


_UNIT_RE = re.compile(r"^[\s\-/]*(?:per\s+)?([a-z]+)", re.IGNORECASE)


def _unit_after(after: str) -> str | None:
    m = _UNIT_RE.match(after)
    if not m:
        return None
    folded = _fold(m.group(1).lower())
    concept = _SYNONYMS.get(folded, folded)
    return concept if concept in _UNIT_CONCEPTS else None


# Comparators written AFTER the figure (C7R review): "15% or more", "3 hours or less",
# "6 hours and up", "$300 or under".  Up to two words (the unit: "hours", "per night") may sit
# between the figure and the comparator.  "and up to" is a range, not "and up".
_POST_GT_RE = re.compile(
    r"^\s*(?:[a-z]+\s+){0,2}?(?:or|and)\s+"
    r"(?:more|greater|higher|above|over|longer|beyond|up\b(?!\s+to))\b",
    re.IGNORECASE,
)
_POST_LT_RE = re.compile(
    r"^\s*(?:[a-z]+\s+){0,2}?(?:or|and)\s+(?:less|fewer|lower|below|under|shorter)\b",
    re.IGNORECASE,
)


def _direction(prefix: str, suffix: str = "") -> str | None:
    """Comparator of a figure: one in front of it wins, else one right after it."""
    tail = prefix[-32:]
    if _LT_RE.search(tail):
        return "<"
    if _GT_RE.search(tail):
        return ">"
    head = suffix[:40]
    if _POST_LT_RE.search(head):
        return "<"
    if _POST_GT_RE.search(head):
        return ">"
    return None


def number_facts(text: str) -> tuple[NumberFact, ...]:
    """Every figure in *text* with its kind and comparator (deterministic, in text order)."""
    prepared = _prepare(text)
    found: list[tuple[int, NumberFact]] = []
    for m in _NUMBER_RE.finditer(prepared):
        value = _canon_number(m.group("int"), m.group("frac"))
        before, after = prepared[: m.start()], prepared[m.end() :]
        if m.group("pct"):
            kind, value = "pct", f"{value}%"
        elif _MONEY_BEFORE_RE.search(before[-6:]) or _MONEY_AFTER_RE.search(after):
            kind = "money"
        else:
            kind = "qty"
        found.append(
            (m.start(), NumberFact(value, kind, _direction(before, after), _unit_after(after)))
        )
    for m in _NUMBER_WORD_RE.finditer(prepared):
        value = str(_NUMBER_WORDS[m.group("word").lower()])
        pct = bool(m.group("pct"))
        found.append(
            (
                m.start(),
                NumberFact(
                    f"{value}%" if pct else value,
                    "pct" if pct else "qty",
                    _direction(prepared[: m.start()], prepared[m.end() :]),
                    _unit_after(prepared[m.end() :]),
                ),
            )
        )
    return tuple(fact for _, fact in sorted(found, key=lambda pair: pair[0]))


def _number_satisfied(num: str, pool: frozenset[str]) -> bool:
    """Exact match, or a BARE sentence figure backed by the same evidence percentage.

    "more than 15 percent" is canonicalised to "15%", but a sentence may say "by 15 or more"
    while the clause says "15%": the same figure, accepted.  The reverse is NOT accepted — a
    "20%" claim is never satisfied by an evidence "$20".
    """
    if num in pool:
        return True
    return not num.endswith("%") and f"{num}%" in pool


def concepts(text: str) -> tuple[str, ...]:
    """Ordered, de-duplicated content concepts of *text* (folded, synonym-mapped, no numbers)."""
    words = _WORD_RE.findall(_prepare(text).lower())
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in _NEGATION_CUES or word.isdigit() or word in _NUMBER_WORDS:
            continue
        if word in _SUPPORT_STOP_WORDS:
            continue
        folded = _fold(word)
        if folded in _STOP_FOLDED or not folded:
            continue
        concept = _SYNONYMS.get(folded, folded)
        if concept not in seen:
            seen.add(concept)
            out.append(concept)
    return tuple(out)


_PAREN_RE = re.compile(r"\([^()]*\)")
# A bound written with a negation word is a comparator, not a negated statement: "allowed up to
# $75 and NO more than 3 times per quarter" is a positive rule.  (The figure's direction is
# checked by the direction guard, which reads "no more than" as "<".)
_NEGATED_BOUND_RE = re.compile(
    r"\b(?:no|not)\s+(?:more|less|fewer|later|earlier|greater|longer)\s+than\b", re.IGNORECASE
)


def _is_negated(text: str) -> bool:
    """Statement-level negation.  A parenthetical condition ("one daily breakfast (if not in per
    diem) ... are reimbursable") does not negate the statement around it, so it is ignored, and
    neither does a negated bound ("no more than 3 times")."""
    prepared = _NEGATED_BOUND_RE.sub(" ", _PAREN_RE.sub(" ", _prepare(text)))
    words = _WORD_RE.findall(prepared.lower())
    return any(w in _NEGATION_CUES for w in words)


def has_qualifier(text: str) -> bool:
    """True when *text* restricts, negates or states a requirement / condition — a potential
    caveat for the surrounding answer ("only ...", "unless ...", "you must get VP approval")."""
    words = _WORD_RE.findall(_prepare(text).lower())
    for word in words:
        if word in QUALIFIER_CUES or word in _CONDITION_WORDS:
            return True
        folded = _fold(word)
        if _SYNONYMS.get(folded, folded) in _CONDITION_CONCEPTS:
            return True
    return False


# ---------------------------------------------------------------------------- the check


@dataclass(frozen=True)
class SentenceSupport:
    """Verdict for one answer sentence (deterministic; JSON-shaped via ``to_dict``)."""

    index: int
    score: float  # content-concept recall against the cited evidence, in [0, 1]
    supported: bool  # cited AND score >= threshold AND every hard guard passed
    substantive: bool  # carries >= 1 content concept or number (not just "Yes [§X].")
    qualifier: bool  # restricts / negates — dropping it may change the answer's meaning
    cited_refs: tuple[str, ...]
    # gate names: uncited | low_support | number | direction | unanchored | polarity | contrast
    #             | verdict
    failed: tuple[str, ...] = ()
    missing_numbers: tuple[str, ...] = ()
    unsupported_terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "score": round(self.score, 4),
            "supported": self.supported,
            "substantive": self.substantive,
            "qualifier": self.qualifier,
            "cited_refs": list(self.cited_refs),
            "failed": list(self.failed),
            "missing_numbers": list(self.missing_numbers),
            "unsupported_terms": list(self.unsupported_terms),
        }


@dataclass(frozen=True)
class SupportInput:
    """One sentence to check: its text, the refs it cites, and those clauses' text."""

    text: str
    cited_refs: tuple[str, ...]
    evidence: tuple[str, ...]  # leaf text of each cited clause
    # The ref each ``evidence`` entry belongs to (aligned with ``evidence``).  Empty -> taken to
    # be ``cited_refs`` when the two have the same length (else no evidence unit is known).
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupportContext:
    """Answer-level context shared by every sentence (question premise + sibling clauses)."""

    question: str = ""
    # (ref, leaf text) of every retrieved clause — used by the contrast guard only.
    retrieved: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Refs of retrieved DERIVATIVE units (FAQ / How-To / Reference): they restate the policy
    # clauses they name.  Empty (unknown) -> no unit is treated as a restatement (fail closed).
    derivative_refs: frozenset[str] = frozenset()
    # Calibrated (live reranker) path: a cited sentence that is ONLY a yes/no verdict fails
    # outright (``verdict``) instead of borrowing the polarity of the statement after it.
    strict_verdicts: bool = False


def _segments(evidence: Iterable[str]) -> list[str]:
    segs: list[str] = []
    for text in evidence:
        for part in _SEGMENT_SPLIT_RE.split(_prepare(text)):
            if part and part.strip():
                segs.append(part.strip())
    return segs


# Polarity is read per STATEMENT.  An FAQ unit opens with the user's question ("Q: Can I take
# Uber Black?") — a question asserts nothing, so it has no polarity — and one clause sentence can
# join a permission and a prohibition ("visa costs are reimbursable ... while personal overages
# are not"; "do not require meal receipts but do require travel dates"): each half is its own
# statement.  "... and not pre-convert it" starts a negated statement of its own as well.
_STATEMENT_SPLIT_RE = re.compile(
    r"(?<=[.;!?])\s+|\s[—–]\s|,?\s+(?:while|whereas|but)\s+|,?\s+and\s+(?=(?:not|never)\b)",
    re.IGNORECASE,
)


def _statement_segments(evidence: Iterable[str]) -> list[str]:
    segs: list[str] = []
    for text in evidence:
        for part in _STATEMENT_SPLIT_RE.split(_prepare(text)):
            part = part.strip()
            if not part or part.endswith("?"):
                continue
            segs.append(part.rstrip(".;!").strip())
    return [s for s in segs if s]


def _concept_set(text: str) -> frozenset[str]:
    return frozenset(concepts(text))


_PREDICATE_CONCEPTS: frozenset[str] = frozenset(
    _SYNONYMS.get(_fold(w), _fold(w))
    for w in (
        "reimbursable",
        "reimbursed",
        "permitted",
        "allowed",
        "covered",
        "approval",
        "approved",
        "expense",
        "claim",
        "fee",
        "fees",
        "charge",
        "charges",
    )
)
# A sentence that OPENS with a yes/no verdict.  "No" is a verdict only when punctuation follows
# it ("No.", "No, ...", "No — ..."): "No fees apply" opens with the determiner "no".
_VERDICT_OPEN_RE = re.compile(
    r"^\W*(?P<word>yes\b|no(?=\s*(?:[,.;:!—–-]|$)))[\s,.;:!—–-]*", re.IGNORECASE
)
# Words that only affirm a verdict ("Yes, that is CORRECT", "Yes, absolutely").
_VERDICT_FILLER: frozenset[str] = frozenset(
    _SYNONYMS.get(_fold(w), _fold(w))
    for w in (
        "correct",
        "right",
        "true",
        "fine",
        "acceptable",
        "sure",
        "absolutely",
        "indeed",
        "definitely",
        "certainly",
        "possible",
    )
)


def is_verdict_only(text: str) -> bool:
    """True when *text* is a yes/no verdict and nothing else (citation markers ignored).

    "Yes." / "No." / "Yes, you can." / "No, you cannot." / "Yes, that is allowed." — after
    the leading "Yes" / "No" the sentence names no subject and no figure: at most generic
    predicate words (allowed, reimbursable, approval, ...) or affirmations (correct, sure).
    Such a sentence asserts that the QUESTION's premise holds, which no clause text can verify,
    so it cannot carry a citation of its own.  "No receipts are needed" is not a verdict (the
    body names "receipts"), nor is "No fees apply" ("No" without punctuation is a determiner).
    """
    prepared = _prepare(text)
    m = _VERDICT_OPEN_RE.match(prepared)
    if m is None:
        return False
    return is_claimless(prepared[m.end() :])


# Words a bare verdict is made of: generic predicates (allowed, reimbursable, approval, ...) and
# affirmations (correct, sure, ...).
_VERDICT_WORDS: frozenset[str] = _PREDICATE_CONCEPTS | _VERDICT_FILLER


def is_claimless(text: str) -> bool:
    """True when *text* names no figure and no content concept beyond verdict words — "You
    can.", "It is permitted.", "That is allowed." — whatever its first word."""
    return not numbers_in(text) and all(c in _VERDICT_WORDS for c in concepts(text))


def carries_claim(text: str) -> bool:
    """True when *text* states something a clause could back: a content concept or a figure,
    and is not merely a yes/no verdict (``is_verdict_only``).  Citation markers are ignored."""
    return (bool(concepts(text)) or bool(numbers_in(text))) and not is_verdict_only(text)


def _asserted_concepts(evidence: Iterable[str]) -> frozenset[str]:
    """Concepts the evidence ASSERTS: every statement except a question line ("Q: Can I take
    Uber Black?" in an FAQ unit names Uber Black without saying anything about it)."""
    out: set[str] = set()
    for seg in _statement_segments(evidence):
        out.update(concepts(seg))
    return frozenset(out)


_FAQ_VERDICT_RE = re.compile(r"(?:^|\n)\s*A:\s*(?P<word>yes|no)\b", re.IGNORECASE)


def _echo_verdict_ok(sentence: str, evidence: Sequence[str]) -> bool:
    """May a sentence that echoes an FAQ QUESTION line stand on that unit (anchor guard)?

    Only with the polarity of the FAQ's own answer verdict ("A: No — ..."): every verdict the
    evidence opens an answer with must match the sentence (negated <-> "No").  With no explicit
    verdict to compare against, only a negated sentence passes.
    """
    negated = _is_negated(_LEADING_ANSWER_RE.sub("", sentence, count=1))
    verdicts = {m.group("word").lower() for e in evidence for m in _FAQ_VERDICT_RE.finditer(e)}
    if not verdicts:
        return negated
    return verdicts == {"no" if negated else "yes"}


def _polarity_ok(sentence: str, evidence: Sequence[str]) -> bool:
    """Sentence negation must match the best-overlapping evidence segment(s), per statement.

    Ties between a negated and a positive segment are resolved ASYMMETRICALLY: a NEGATED
    sentence passes if any tied segment is negated, but a POSITIVE sentence must not tie with a
    negated segment.  "First class is reimbursable for flights > 10 hours [§AIR-03]" overlaps
    the positive Business-class segment and "First class is never reimbursable" equally — the
    permissive reading would let the flip through.  Wrongly asserting a permission is the
    costly error (an employee spends money), so ambiguity fails the positive claim.
    """
    body = _LEADING_ANSWER_RE.sub("", sentence, count=1)
    segments = [(seg, _concept_set(seg), _is_negated(seg)) for seg in _statement_segments(evidence)]
    # A sentence can join statements of both polarities too ("an eSIM is reimbursable, but
    # personal overages are not"): each statement must match the evidence statement it restates.
    return all(
        _statement_polarity_ok(statement, segments)
        for statement in _STATEMENT_SPLIT_RE.split(_prepare(body))
    )


def _statement_polarity_ok(
    statement: str, segments: Sequence[tuple[str, frozenset[str], bool]]
) -> bool:
    sent_concepts = set(concepts(statement))
    if not sent_concepts:
        return True
    best = 0
    tied: list[tuple[float, bool]] = []  # (coverage, negated) of the best-overlap segments
    for _seg, seg_concepts, negated in segments:
        overlap = len(sent_concepts & seg_concepts)
        if overlap == 0 or overlap < best:
            continue
        if overlap > best:
            best, tied = overlap, []
        tied.append((overlap / len(seg_concepts), negated))
    if best == 0:
        return True  # nothing to compare against; recall decides
    if _is_negated(statement):
        return any(negated for _cov, negated in tied)
    # A POSITIVE statement tied with a negated segment: the segment it restates most closely
    # (largest share of that segment's own concepts) is the one it talks about — "Only
    # personal-card expenses are reimbursed" is all of "only personal-card expenses are
    # reimbursed to you", not of "corporate-card expenses are paid directly with no personal
    # reimbursement".  An exact tie on that too still fails (the asymmetric rule above).
    top = max(cov for cov, _neg in tied)
    if any(negated for cov, negated in tied if cov == top):
        return False
    # Mixed-polarity clause (C7R review): a positive sentence can overlap the clause's
    # POSITIVE segment most while asserting something only its NEGATED segment talks about —
    # "First class is reimbursable for flights > 10 hours with VP approval [§AIR-03]" borrows
    # the Business-class conditions; "Wi-Fi and minibar are reimbursable [§LOD-04]" rides on
    # the Wi-Fi half.  Fail when the sentence names a subject concept that appears in a negated
    # segment and in NO positive segment of the cited evidence.  Generic predicate concepts
    # (reimbursable, permitted, approval, ...) are not subjects.
    positive: set[str] = set()
    negative: set[str] = set()
    for _seg, seg_concepts, negated in segments:
        (negative if negated else positive).update(seg_concepts)
    only_negated = (sent_concepts & negative) - positive - _PREDICATE_CONCEPTS
    return not only_negated


_REQUIREMENT_RE = re.compile(r"\b(?:must|needs?|require[sd]?|mandatory)\b", re.IGNORECASE)
_REQUIRE_CONCEPT = _SYNONYMS.get(_fold("require"), _fold("require"))


def _matching_concepts(text: str) -> frozenset[str]:
    """Concepts used to pick WHICH evidence segment a sentence talks about.

    Modal requirement words ("need", "must") are stop words for recall — clauses phrase a
    requirement as "with VP approval" as often as "requires" — but they do tell the two halves
    of a threshold rule apart ("≤ 15% are auto-approved" vs "> 15% REQUIRES manager approval").
    """
    found = set(concepts(text))
    if _REQUIREMENT_RE.search(text):
        found.add(_REQUIRE_CONCEPT)
    return frozenset(found)


def _segment_facts(evidence: Sequence[str]) -> list[tuple[frozenset[str], tuple[NumberFact, ...]]]:
    return [(_concept_set(seg), number_facts(seg)) for seg in _segments(evidence)]


def _direction_ok(fact: NumberFact, sent_concepts: frozenset[str], segments: Sequence[Any]) -> bool:
    """A figure stated with a comparator must keep the clause's comparator for that figure.

    "Business class for flights UNDER 10 hours" vs "> 10 hours", or "> 15% ... auto-approved"
    vs "≤ 15% are auto-approved": the figure exists in the clause, the direction is reversed.
    The comparator is read from the evidence segment(s) that contain the figure and overlap the
    sentence most (LOD-02 states 15% twice, ≤ and >, in different segments); a tie among those
    segments passes if any of them agrees.  A clause figure with no comparator imposes none.
    """
    if fact.direction is None:
        return True
    best = -1
    directions: set[str | None] = set()
    for seg_concepts, facts in segments:
        matching = {f.direction for f in facts if f.value == fact.value}
        if not matching:
            continue
        overlap = len(sent_concepts & seg_concepts)
        if overlap > best:
            best, directions = overlap, set(matching)
        elif overlap == best:
            directions |= matching
    if best < 0 or None in directions:
        return True
    return fact.direction in directions


def _as_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.rstrip("%"))
    except InvalidOperation:  # pragma: no cover — canonical values are numeric
        return None


def _threshold_ok(
    fact: NumberFact, sent_concepts: frozenset[str], segments: Sequence[Any]
) -> bool | None:
    """Is a PREMISE figure consistent with the cited threshold it is applied to?

    "Since your rate is 20% over the cap, you need manager approval [§LOD-02]" — 20% is the
    user's figure, LOD-02 states thresholds "≤ 15% auto-approved" / "> 15% requires manager
    approval".  The premise is compared with the thresholded figures (same kind, compatible
    unit) of the evidence segment(s) that overlap the sentence most; it must satisfy at least
    one of them (inclusive at equality — "≤" and "<" are not distinguished).  Returns None when
    the evidence states no such threshold (nothing to compare against).
    """
    value = _as_decimal(fact.value)
    if value is None:
        return None
    best = -1
    thresholds: list[NumberFact] = []
    for seg_concepts, facts in segments:
        cands = [
            f
            for f in facts
            if f.direction is not None
            and f.kind == fact.kind
            and (f.unit is None or fact.unit is None or f.unit == fact.unit)
        ]
        if not cands:
            continue
        overlap = len(sent_concepts & seg_concepts)
        if overlap > best:
            best, thresholds = overlap, cands
        elif overlap == best:
            thresholds.extend(cands)
    if not thresholds:
        return None
    for t in thresholds:
        limit = _as_decimal(t.value)
        if limit is None:
            continue
        if (t.direction == "<" and value <= limit) or (t.direction == ">" and value >= limit):
            return True
    return False


def _rules_stated(ref: str, text: str) -> frozenset[str]:
    """The policy rule(s) one unit states: the clause ids its text names, else its own id.

    FAQ / How-To / Reference units restate policy clauses and name them inline ("A: Yes —
    economy is required ... (see AIR-01)" -> {AIR-01}; HOW-07 -> {AIR-01, AIR-04, AIR-05,
    AIR-06}); a policy clause names only itself ("AIR-03 Business class: ..." -> {AIR-03}).
    """
    named = set(_CLAUSE_ID_RE.findall(text or "")) - {ref}
    return frozenset(named or {ref})


def _restates_cited(
    ref: str, text: str, cited_rules: frozenset[str], derivative_refs: frozenset[str]
) -> bool:
    """True when a retrieved unit is a restatement of the cited rule(s), not a different rule.

    Either a DERIVATIVE unit (FAQ / How-To / Reference) that names ONLY rules the cited
    clause(s) state — subset, not overlap: HOW-07 names AIR-01 but also the premium-economy /
    business rules (AIR-04), so it is NOT a restatement of AIR-01 and stays a contrast sibling —
    or the very policy clause a cited derivative unit names (FAQ-Q07 "... (see AIR-07)" and
    AIR-07).  A policy clause that merely cross-references the cited one ("CONF-02 ... (see
    LOD-02)") is its own rule.
    """
    if ref in cited_rules:
        return True
    return ref in derivative_refs and _rules_stated(ref, text) <= cited_rules


def assess_sentence(
    index: int,
    item: SupportInput,
    ctx: SupportContext,
    *,
    threshold: float,
) -> SentenceSupport:
    """Score one sentence against the clause text it cites (pure; never raises)."""
    sent_concepts = concepts(item.text)
    sent_numbers = numbers_in(item.text)
    # A yes/no verdict and nothing else ("Yes, you can.") states no claim of its own.
    substantive = (bool(sent_concepts) or bool(sent_numbers)) and not is_verdict_only(item.text)
    qualifier = has_qualifier(_LEADING_ANSWER_RE.sub("", item.text, count=1))

    if not item.cited_refs or not any(e.strip() for e in item.evidence):
        return SentenceSupport(
            index=index,
            score=0.0 if substantive else 1.0,
            supported=False,
            substantive=substantive,
            qualifier=qualifier,
            cited_refs=item.cited_refs,
            failed=("uncited",),
            unsupported_terms=sent_concepts,
        )

    evidence_text = " ".join(item.evidence)
    ev_concepts = set(concepts(evidence_text))
    q_concepts = set(concepts(ctx.question))
    sent_set = frozenset(sent_concepts)
    match_set = _matching_concepts(item.text)

    supported_terms = [c for c in sent_concepts if c in ev_concepts]
    unsupported = [c for c in sent_concepts if c not in ev_concepts]
    # Premise echo: an unsupported concept the user's question already contains ("domestic" in
    # "Economy for a 3-hour DOMESTIC flight") is premise, not a policy claim, and is left out of
    # the denominator — BUT only for an ANCHORED sentence (below).  A sentence made of question
    # words alone never passes on echo: that is how "Yes, you can expense your gym membership
    # while traveling [§MEAL-01]" would otherwise score 1.0 against any clause.
    echo_unsupported = [c for c in unsupported if c in q_concepts]
    claim_unsupported = [c for c in unsupported if c not in q_concepts]
    denom = len(supported_terms) + len(claim_unsupported)
    if denom:
        score = len(supported_terms) / denom
    else:
        # No concept at all (a bare "Yes [§X]." or a figure-only fragment): recall is vacuous —
        # the numeric / anchor guards decide.  Concepts that are ALL unsupported echo: 0.
        score = 0.0 if sent_concepts else 1.0

    failed: list[str] = []
    if score < threshold:
        failed.append("low_support")

    # Numeric guard (hard).  A figure must be in the cited evidence.  The ONLY exception is a
    # PREMISE figure from the user's question ("a 12-hour flight", "your rate is 20% over the
    # cap"), accepted narrowly:
    #   * it carries NO comparator in the sentence — "flights OVER 12 hours" states a threshold,
    #     i.e. a policy claim, and must be cited;
    #   * it is consistent with the cited threshold it is applied to (``_threshold_ok``) — "a
    #     20% overage is auto-approved [§LOD-02]" fails against "≤ 15% auto-approved";
    #   * money / percentages additionally need something cited to be compared against: a
    #     cited same-kind figure in the sentence, or a cited same-kind threshold.  So "Yes, the
    #     Tier 1 cap is $500 per night [§LOD-01]" (the user's wrong $500 echoed back) fails —
    #     LOD-01's amounts are values, not thresholds — whatever the question said.
    ev_numbers = numbers_in(evidence_text)
    q_numbers = numbers_in(ctx.question)
    facts = number_facts(item.text)
    segments = _segment_facts(item.evidence)
    cited_kinds = {
        f.kind
        for f in facts
        if _number_satisfied(f.value, ev_numbers) and not _number_satisfied(f.value, q_numbers)
    }
    missing_set: set[str] = set()
    for f in facts:
        if _number_satisfied(f.value, ev_numbers):
            continue
        premise_ok = False
        if _number_satisfied(f.value, q_numbers) and f.direction is None:
            consistent = _threshold_ok(f, match_set, segments)
            premise_ok = consistent is not False and (
                f.kind == "qty" or f.kind in cited_kinds or consistent is True
            )
        if not premise_ok:
            missing_set.add(f.value)
    missing = tuple(sorted(missing_set))
    if missing:
        failed.append("number")

    # Direction guard (hard): a cited figure keeps the clause's comparator.
    if any(
        _number_satisfied(f.value, ev_numbers) and not _direction_ok(f, match_set, segments)
        for f in facts
    ):
        failed.append("direction")

    # Anchor guard (hard): the sentence must say something the CITED clause backs beyond the
    # user's own words — a supported non-question concept, or a cited figure the question did
    # not supply.  A pure restatement of the question passes only if the clause covers ALL of
    # it (e.g. "No, minibar charges are not reimbursable [§LOD-04]" — every word is in LOD-04;
    # the polarity guard then checks yes vs no).  "Backs" means a clause STATEMENT names it: an
    # FAQ unit's question line asserts nothing, so "Yes, you can take Uber Black [§FAQ-Q45]"
    # (Q: "Can I take Uber Black ...?"  A: "No — ... premium tiers are not reimbursable") is an
    # echo of the FAQ's question, not of its answer: such an echo is anchored only when it
    # takes the FAQ answer's own verdict ("No, you cannot take Uber Black" against "A: No —"),
    # or — when that answer opens with no verdict — only as a NEGATIVE statement (wrongly
    # asserting a permission is the costly error).
    asserted = _asserted_concepts(item.evidence)
    backed = [c for c in supported_terms if c in asserted]
    anchored = (
        any(c not in q_concepts for c in backed)
        or bool(cited_kinds)
        or (
            bool(supported_terms)
            and not echo_unsupported
            and (len(backed) == len(supported_terms) or _echo_verdict_ok(item.text, item.evidence))
        )
    )
    if substantive and not anchored:
        failed.append("unanchored")

    if not _polarity_ok(item.text, item.evidence):
        failed.append("polarity")

    # Contrast guard (hard): mis-attribution.  A claim concept the cited clause lacks but a
    # DIFFERENT retrieved clause contains ("book Business class [§AIR-01]" — "business" is
    # AIR-03's).  For a concept echoed from the question the bar is higher (the question may
    # name a setting the cited rule simply does not repeat, e.g. "domestic" also appears in
    # PTA-02): it is a contrast hit only when that sibling matches the sentence AT LEAST AS WELL
    # as the cited clause does — i.e. the answer applied the question's subject to the wrong
    # clause ("Yes, business class is standard for flights under 6 hours [§AIR-01]").
    #
    # A RESTATEMENT of the cited rule is not a different rule: FAQ / How-To / Reference units
    # restate a policy clause and name it ("... (see AIR-03)").  A retrieved unit that states
    # ONLY rules the cited clause(s) state (``_restates_cited``) is never a contrast sibling, and
    # a concept it states counts as the cited rule's own wording for this guard — "longer" taken
    # from FAQ-Q01 while citing AIR-03 is the same rule, not a borrowed one.  (Recall / score,
    # numbers, direction and polarity are still read from the CITED clause only.)
    # The rules the sentence cites: its own refs, plus the policy clauses a cited DERIVATIVE
    # unit restates and names ("... (see AIR-03)").  A cited POLICY clause contributes only its
    # own ref — "CARD-03 ... within the EXP-01 deadline" cross-references EXP-01, it does not
    # restate it, so EXP-01's wording borrowed under a CARD-03 citation is still contrast.
    cited = set(item.cited_refs)
    evidence_refs = item.evidence_refs or (
        item.cited_refs if len(item.cited_refs) == len(item.evidence) else ()
    )
    cited_rules = frozenset(cited).union(
        *(
            _CLAUSE_ID_RE.findall(text)
            for ref, text in zip(evidence_refs, item.evidence, strict=False)
            if ref in ctx.derivative_refs
        )
    )
    siblings: list[frozenset[str]] = []
    restated: set[str] = set()
    for ref, text in ctx.retrieved:
        if ref in cited:
            continue
        if _restates_cited(ref, text, cited_rules, ctx.derivative_refs):
            restated.update(_concept_set(text))
        else:
            siblings.append(_concept_set(text))
    cited_overlap = len(supported_terms)
    contrast: list[str] = []
    for sib in siblings:
        contrast.extend(c for c in claim_unsupported if c in sib and c not in restated)
        if len(sent_set & sib) >= cited_overlap:
            contrast.extend(c for c in echo_unsupported if c in sib and c not in restated)
    if contrast:
        failed.append("contrast")

    # Verdict guard (strict / calibrated path only).  A cited sentence that states nothing a
    # clause could back fails outright: one made of verdict words alone ("You can.", "It is
    # permitted.", "That is allowed." — whatever its first word), or one that only echoes
    # PART of the question plus verdict words ("Yes, you can fly [§AIR-03]." to "Can I fly
    # business class on a 3-hour flight?").  Either asserts that the question's premise holds,
    # which no clause text verifies.  A sentence that adds at least one BACKED concept or cited
    # figure the question did not supply ("... only for flights over 10 hours") stays
    # substantive.  So does an echo that restates the question's WHOLE subject ("No, minibar
    # and in-room movie fees are not reimbursable [§FAQ-Q17]"): it names everything the
    # question asked about, so the anchor, number and polarity guards above can check it
    # against the clause — a partial echo drops the very premise ("business class", "3-hour")
    # those guards would test.
    if ctx.strict_verdicts:
        adds_figure = any(
            _number_satisfied(n, ev_numbers) and not _number_satisfied(n, q_numbers)
            for n in sent_numbers
        )
        adds_concept = any(c not in q_concepts and c not in _VERDICT_WORDS for c in backed)
        q_subject = q_concepts - _QUESTION_SCAFFOLD - _VERDICT_WORDS
        partial_echo = not (adds_figure or adds_concept) and not q_subject <= sent_set
        if is_claimless(item.text) or partial_echo:
            failed.append("verdict")
            substantive = False

    return SentenceSupport(
        index=index,
        score=score,
        supported=not failed,
        substantive=substantive,
        qualifier=qualifier,
        cited_refs=item.cited_refs,
        failed=tuple(failed),
        missing_numbers=missing,
        unsupported_terms=tuple(claim_unsupported + echo_unsupported),
    )


# A wh-question, also when it opens with a preposition ("By when ...", "For how long ...",
# "In which ...", "To whom ...", "Under what ...").
_WH_QUESTION_RE = re.compile(
    r"^\W*(?:(?:by|for|in|to|under|from|at|on|with|within|until|till|of|after|before|during"
    r"|since|about|into|through)\s+)?(?:what|which|who|whom|whose|where|when|why|how)\b",
    re.IGNORECASE,
)


def _bare_answer_word(text: str) -> str | None:
    """Return "yes" / "no" when the sentence is NOTHING but that verdict (markers stripped):
    "Yes." as well as "Yes, you can." / "No, you cannot." (``is_verdict_only``)."""
    if not is_verdict_only(text):
        return None
    m = _VERDICT_OPEN_RE.match(_prepare(text))
    return m.group("word").lower() if m else None


def _check_bare_answers(
    items: Sequence[SupportInput],
    verdicts: list[SentenceSupport],
    question: str,
    *,
    strict: bool = False,
) -> None:
    """A bare "Yes." / "No." (or "Yes, you can.") sentence must agree with the statement it
    introduces (C7R).

    It carries no concept and no figure, so recall scores it 1.0 and the polarity guard has no
    body to read: on its own it would pass whatever it asserts.  "Yes. [§LOD-04] Minibar charges
    are not reimbursable [§LOD-04]." must not read as a grounded "yes".  It is supported only
    when the NEXT substantive sentence exists, is itself supported, and has the same polarity
    (a "No" introduces a negated statement, a "Yes" a positive one), and never as the answer
    to a wh-question ("What is the NYC hotel cap?" has no yes/no answer).  In place; the failed
    gate is recorded as ``polarity``.

    With *strict* (the calibrated path) a cited verdict-only sentence fails outright with
    ``verdict``: agreeing in POLARITY with the next statement does not make the verdict right —
    "Can I fly business class on a 3-hour flight?" -> "Yes [§AIR-01]. Economy is the standard
    for flights under 6 hours [§AIR-01]." pairs a true positive statement with a wrong "Yes".
    The verified statements after it can still be shown provisionally.
    """
    wh_question = bool(_WH_QUESTION_RE.match(question or ""))
    for i, item in enumerate(items):
        word = _bare_answer_word(item.text)
        if word is None or not verdicts[i].supported:
            continue
        if strict:
            verdicts[i] = replace(
                verdicts[i], supported=False, failed=(*verdicts[i].failed, "verdict")
            )
            continue
        follower = next((j for j in range(i + 1, len(items)) if verdicts[j].substantive), None)
        agrees = (
            follower is not None
            and verdicts[follower].supported
            and _is_negated(_LEADING_ANSWER_RE.sub("", items[follower].text, count=1))
            == (word == "no")
        )
        if wh_question or not agrees:
            verdicts[i] = replace(
                verdicts[i], supported=False, failed=(*verdicts[i].failed, "polarity")
            )


def assess_support(
    items: Sequence[SupportInput],
    ctx: SupportContext | None = None,
    *,
    threshold: float,
) -> tuple[SentenceSupport, ...]:
    """Assess every sentence in answer order (pure, deterministic, never raises)."""
    context = ctx or SupportContext()
    verdicts = [
        assess_sentence(i, item, context, threshold=threshold) for i, item in enumerate(items)
    ]
    _check_bare_answers(items, verdicts, context.question, strict=context.strict_verdicts)
    return tuple(verdicts)


# ------------------------------------------------------------------ question relevance (C7R)

# Interrogative / request scaffolding in a QUESTION.  It frames WHAT is asked ("how LONG do I
# have", "what HAPPENS if", "the RULE on", "do I NEED a ...", "is that ALLOWED") and is never
# the subject an answer has to cover, so it is left out of the question's concepts.  Generic
# words only — never a T&E subject noun.
_QUESTION_SCAFFOLD_WORDS: tuple[str, ...] = (
    "many",
    "much",
    "long",
    "soon",
    "quick",
    "quickly",
    "happen",
    "happens",
    "rule",
    "rules",
    "one",
    "ones",
    "now",
    "know",
    "very",
    "extra",
    "get",
    "got",
    "given",
    "enough",
    "ready",
    "step",
    "steps",
    "way",
    "ways",
    "thing",
    "things",
    "kind",
    "type",
    "types",
    "exactly",
    "actually",
    "really",
    "need",
    "needs",
    "require",
    "required",
    "special",
    "specific",
    "particular",
    # "Is that ALLOWED?" asks for the answer's polarity, which the support check verifies.
    "allow",
    "allowed",
    "permit",
    "permitted",
)
_QUESTION_SCAFFOLD: frozenset[str] = frozenset(
    _SYNONYMS.get(_fold(w), _fold(w)) for w in _QUESTION_SCAFFOLD_WORDS
)
MIN_QUESTION_CONCEPTS = 2
"""A question naming fewer subject concepts ("What's the cap?", "Is it reimbursable?") is too
vague for any clause to be known to answer it."""


@dataclass(frozen=True)
class QuestionRelevance:
    """Does the answer address what the QUESTION asks about, with cited backing? (C7R)"""

    relevant: bool
    concepts: tuple[str, ...]  # the question's subject concepts (scaffolding removed)
    uncovered: tuple[str, ...]  # absent from the cited clause text
    unaddressed: tuple[str, ...]  # absent from the answer text itself
    unmatched_numbers: tuple[str, ...] = ()  # figures in the question absent from the answer
    polar_verdict: bool = False  # a sentence of the answer opens with a yes/no verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "relevant": self.relevant,
            "concepts": list(self.concepts),
            "uncovered": list(self.uncovered),
            "unaddressed": list(self.unaddressed),
            "unmatched_numbers": list(self.unmatched_numbers),
            "polar_verdict": self.polar_verdict,
        }


# A sentence that opens with a yes/no VERDICT: "Yes", "Yes, ...", "No.", "No, ...", "No - ...".
# "No receipts are needed" opens with the determiner "no", not a verdict, so a "No" verdict needs
# punctuation right after it.
_POLAR_VERDICT_RE = re.compile(r"^\W*(?:yes\b|no\s*[,.;:!—–-])", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def has_polar_verdict(answer: str) -> bool:
    """True when any sentence of *answer* opens with a yes/no verdict (citation markers ignored)."""
    return any(
        _POLAR_VERDICT_RE.match(_prepare(sentence))
        for sentence in _SENTENCE_SPLIT_RE.split(answer or "")
    )


def question_relevance(question: str, answer: str, evidence: Iterable[str]) -> QuestionRelevance:
    """Answer-vs-QUESTION relevance: the answer AND its cited clauses cover the question's subject.

    The sentence-support check verifies each sentence against the clause it cites; it cannot see
    that a faithful answer is about something else.  "What life-insurance coverage does the
    company provide?" answered with "The company provides business travel insurance
    [§INTL-04]" is a true, cited, entailed sentence — but neither the answer nor the clause it
    cites mentions "life": the question was not answered.  Likewise "Domestic US cities have a
    cap of USD 180 per night [§HOTEL-CAP]" for a Tier-1 question cites a clause that mentions
    Tier-1, yet the answer never addresses it.  Relevant only when the question names at least
    ``MIN_QUESTION_CONCEPTS`` subject concepts and EVERY one of them appears both in *answer* and
    in *evidence* (folded, synonym-mapped — the concept space of the support check).

    Concepts cannot see the question's PREMISE.  "Can I book business class for a 2-hour
    flight?" answered "Yes, business class is permitted for flights over 10 hours [§AIR-03]"
    covers every concept and cites a clause that says exactly that — and the "Yes" is wrong.
    Nothing downstream compares a yes/no verdict with the question (the critic never sees it),
    so the answer is also NOT relevant when
      * a figure in the question ("2", "$40", "Tier 1") is absent from the answer
        (``unmatched_numbers``): the answer did not speak to the case that was asked; or
      * a sentence opens with a yes/no verdict (``polar_verdict``): on a polar question it
        asserts that the question's premise is met, which nothing here verifies; on a
        wh-question ("By when must I submit ...?" -> "Yes, you must ...") it is no answer.
    Pure; never raises.
    """
    q_concepts = tuple(c for c in concepts(question) if c not in _QUESTION_SCAFFOLD)
    ev_concepts = set(concepts(" ".join(evidence)))
    answer_concepts = set(concepts(answer))
    uncovered = tuple(c for c in q_concepts if c not in ev_concepts)
    unaddressed = tuple(c for c in q_concepts if c not in answer_concepts)
    answer_numbers = numbers_in(answer)
    unmatched_numbers = tuple(
        sorted(n for n in numbers_in(question) if not _number_satisfied(n, answer_numbers))
    )
    polar_verdict = has_polar_verdict(answer)
    return QuestionRelevance(
        relevant=(
            len(q_concepts) >= MIN_QUESTION_CONCEPTS
            and not uncovered
            and not unaddressed
            and not unmatched_numbers
            and not polar_verdict
        ),
        concepts=q_concepts,
        uncovered=uncovered,
        unaddressed=unaddressed,
        unmatched_numbers=unmatched_numbers,
        polar_verdict=polar_verdict,
    )


# Phrasings with which a model declines to answer from the supplied clauses.  Used ONLY to
# classify a draft that has no supported sentence as an abstention (out-of-corpus) rather than
# a "needs review" answer — it never causes text to be shown.  Every pattern must refer to the
# SOURCE (policy / clauses / documents / context) or to answering itself: a policy statement
# such as "alcohol is not covered under per diem" is a negative RULE, not a refusal, and must
# never turn an in-corpus question into "not covered by the documents".
_SOURCE = (
    r"(?:polic(?:y|ies)|clauses?|documents?|documentation|context|sources?|excerpts?|text"
    r"|information|guidelines?|materials?)"
)
_REFUSAL_RE = re.compile(
    r"\b(?:"
    rf"{_SOURCE}\b[^.;]{{0,40}}?\b(?:do|does|did)\s*(?:not|n't)\s+"
    r"(?:cover|address|mention|specify|include|contain|discuss|say|state|provide|answer)"
    rf"|not\s+(?:covered|addressed|mentioned|specified|included|stated|found)\s+"
    rf"(?:in|by)\s+(?:the\s+|any\s+)?(?:[\w&/-]+\s+){{0,3}}?{_SOURCE}"
    rf"|no\s+(?:information|clause|mention|guidance)\b[^.;]{{0,40}}?\b(?:in|from)\b"
    r"|(?:cannot|can't|unable\s+to)\s+(?:answer|find|determine)"
    r"|outside\s+(?:the\s+)?(?:scope|policy)"
    r")",
    re.IGNORECASE,
)


def is_refusal(text: str) -> bool:
    """True when *text* reads as the model declining to answer from the clauses."""
    return bool(_REFUSAL_RE.search(_MARKER_RE.sub(" ", text)))
