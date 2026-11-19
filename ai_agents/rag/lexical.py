"""Query-time lexical relevance over chunk TEXT (C7-10) — deterministic, LLM-free.

Why this exists
---------------
The local embedder (``services/embeddings_provider/local.py``) is a signed feature-hash over
RAW surface tokens: no stemming, no stop-word removal.  "nightly hotel cap" and the clause
"Hotel nightly rate caps ... USD 250 per night" share only ``hotel``/``nightly`` as hash
buckets ("cap" != "caps"), while stop words ("what", "is", "the") inflate the cosine of
unrelated clauses.  An in-corpus clause can therefore come back from the dense path BELOW the
0.20 cosine floor and be culled from the grounding context even though it is the clause the
question is about (staging: HOTEL-CAP at cosine 0.142 behind HOTEL-PREF at 0.270).

The fused sparse path (Postgres ``english`` full-text over ``chunk_tsv``) normally rescues
such a clause, but it is only as good as the stored ``chunk_tsv`` (legacy rows indexed before
R1-T1 carry none).  This module is the query-time, storage-free equivalent: a light,
symmetric normalization (lower-case -> stop-word removal -> conservative suffix stemming)
applied to BOTH the query and the chunk text at query time.  It never touches a stored
vector, so the dense query/document embeddings stay byte-compatible.

Scope (deliberately narrow)
---------------------------
It is used ONLY to decide whether an already-retrieved candidate may stay in the grounding
context (``GroundedRetriever._ground``).  It does NOT feed the answerability gate (that
remains max dense cosine / calibrated tau*), so it cannot turn an abstention into an answer.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z0-9]+")

# Closed-class English function words + question scaffolding.  Content words that carry
# policy meaning (receipt, hotel, cap, meal, taxi, ...) are never listed here.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "me",
        "might",
        "more",
        "must",
        "my",
        "need",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# Ordered (longest first) inflectional suffixes; stripped only when the remaining stem keeps
# >= 3 characters, so short policy words ("bus", "gas", "tax") are left intact.
_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ies", "y"),
    ("ing", ""),
    ("ly", ""),
    ("ed", ""),
    ("es", ""),
    ("s", ""),
)
# "-es" is a plural ending only after a sibilant (taxes, classes, boxes, dishes, matches);
# elsewhere ("expenses", "rates") the "e" belongs to the stem and only "-s" is stripped.
_ES_SIBILANT_ENDINGS: tuple[str, ...] = ("s", "x", "z", "ch", "sh")


def _strip_suffix(token: str) -> str:
    for suffix, replacement in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            if suffix == "s" and token.endswith("ss"):
                return token  # "class", "business" — not a plural
            if suffix == "es" and not token[:-2].endswith(_ES_SIBILANT_ENDINGS):
                continue  # "rates" -> "rate" via the plain "-s" rule below
            return token[: len(token) - len(suffix)] + replacement
    return token


def _stem(token: str) -> str:
    """Conservative, symmetric inflectional stemmer (plural / -ly / -ed / -ing).

    A final silent "e" is dropped afterwards (Porter step 5a style) so the singular and plural
    of -e words meet: "expense"/"expenses" -> "expens", "rate"/"rates" -> "rat".
    """
    if token.isdigit():
        return token
    stem = _strip_suffix(token)
    if stem.endswith("e") and len(stem) >= 4:
        stem = stem[:-1]
    return stem


def content_stems(text: str) -> frozenset[str]:
    """Lower-case, drop stop words, and stem *text* into a set of content stems."""
    return frozenset(_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP_WORDS)


def lexical_overlap(query: str, chunk_text: str, clause_ref: str | None = None) -> int:
    """Number of distinct query content stems present in the chunk (text + clause label).

    The clause label (e.g. ``RECEIPT-REQ`` / ``LOD-01``) is part of the chunk's identity and is
    already indexed by the sparse path's ``chunk_tsv``; it is included here for parity.
    """
    q = content_stems(query)
    if not q:
        return 0
    c = content_stems(f"{chunk_text} {clause_ref or ''}")
    return len(q & c)


def is_lexically_relevant(query: str, chunk_text: str, clause_ref: str | None = None) -> bool:
    """True when the chunk covers a MAJORITY (>= half, and >= 2) of the query's content stems.

    Two independent content concepts (e.g. ``hotel`` + ``cap``) is the minimum that is not a
    coincidental single-word hit; requiring at least half of the query's concepts keeps long
    questions from qualifying on two incidental words.
    """
    q = content_stems(query)
    if len(q) < 2:
        return False
    overlap = lexical_overlap(query, chunk_text, clause_ref)
    return overlap >= 2 and 2 * overlap >= len(q)
