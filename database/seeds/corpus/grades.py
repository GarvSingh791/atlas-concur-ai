"""Traveler-grade corpus text (AIR-04 known limit) — rendered from the ONE ladder.

The grade ladder and its clause-stated entitlements live in
``config/constants/grades.py`` (also read by ``users.grade`` validation and the
AIR-04 rule evaluation); the AIR-04 hour threshold lives in ``figures``.  This
module renders them into the policy clauses DEF-09 / DEF-10 and the FAQ
"Traveler Grades" units, so the RAG corpus and the rule engine describe the same
ladder by construction (``backend/tests/test_kl_grade_ladder.py`` checks it).

The corpus spells "traveler" (US), so the prose does too.
"""

from __future__ import annotations

from config.constants import grades as G
from database.seeds.corpus import figures as F


def _named(g: G.Grade) -> str:
    """ "Senior Vice President (SVP)" — the title, plus the short form when it differs."""
    return g.title if g.short == g.title else f"{g.title} ({g.short})"


def ladder_text() -> str:
    """Every grade, most senior first, separated by " > "."""
    return " > ".join(_named(g) for g in G.GRADES)


def _join(names: list[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    return " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]


def senior_grades_text() -> str:
    """The grades an "SVPs and above" clause covers: "Chief Officer and ..."."""
    return _join([g.title for g in G.grades_at_or_above(G.SENIOR_EXCEPTION_MIN_GRADE)])


def other_grades_text() -> str:
    """The grades below the SVP threshold (no grade-based exceptions)."""
    senior = {g.code for g in G.grades_at_or_above(G.SENIOR_EXCEPTION_MIN_GRADE)}
    return _join([g.title for g in G.GRADES if g.code not in senior])


def entitlements_text() -> str:
    """ "business class on flights > 6 hours (AIR-04); ..." — every clause-stated entitlement."""
    parts: list[str] = []
    for e in G.ENTITLEMENTS:
        if e.clause_id == "AIR-04":
            parts.append(f"business class on flights > {F.SVP_BUSINESS_MIN_HOURS} hours (AIR-04)")
        else:
            parts.append(f"{e.summary} ({e.clause_id})")
    return "; ".join(parts)


def _threshold_short() -> str:
    g = G.grade(G.SENIOR_EXCEPTION_MIN_GRADE)
    if g is None:  # import-time contract: the threshold is a rung of the ladder
        raise RuntimeError("SENIOR_EXCEPTION_MIN_GRADE is not on the grade ladder")
    return g.short


SVP_SHORT: str = _threshold_short()  # "SVP"
