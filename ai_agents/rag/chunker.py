"""Deterministic policy document chunker — pure, hermetic (no DB, no network).

Splits a policy document on [LABEL] markers and produces an ordered list of
PolicyChunk dataclasses.  The preamble before the first marker is discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches markers like [AIR-CABIN], [HOTEL-CAP], [SUSTAIN], etc.
_LABEL_RE: re.Pattern[str] = re.compile(r"\[([A-Z0-9-]+)\]")


@dataclass(frozen=True)
class PolicyChunk:
    """One labelled chunk of a policy document.

    clause_ref  -- the [LABEL] captured from the document (e.g. "AIR-CABIN")
    chunk_text  -- the prose text for that clause, stripped; the [LABEL] tag
                   itself is NOT included in chunk_text.
    """

    clause_ref: str
    chunk_text: str


def chunk_policy_document(document_text: str) -> list[PolicyChunk]:
    """Parse *document_text* into an ordered list of PolicyChunk objects.

    Algorithm
    ---------
    1. Find every [LABEL] marker and its position in the document.
    2. For each marker, the chunk body is the text from after the marker to the
       start of the next marker (or end-of-string).
    3. Strip leading/trailing whitespace from each body.
    4. Discard any preamble before the first marker.

    Returns
    -------
    list[PolicyChunk] in document order, deterministic for a given input.
    """
    matches = list(_LABEL_RE.finditer(document_text))
    if not matches:
        return []

    chunks: list[PolicyChunk] = []
    for i, match in enumerate(matches):
        clause_ref = match.group(1)
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(document_text)
        chunk_text = document_text[body_start:body_end].strip()
        chunks.append(PolicyChunk(clause_ref=clause_ref, chunk_text=chunk_text))

    return chunks
