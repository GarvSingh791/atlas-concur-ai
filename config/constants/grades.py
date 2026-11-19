"""Traveller grade ladder — the SINGLE source of grade codes, titles and rank (AIR-04).

Helios Dynamics' TEP-FY26 grants some travel entitlements by seniority: the clauses
name "SVPs and above" (AIR-04 business class, AIR-07 early-boarding / lounge fees,
LOD-05 suites / room upgrades, NR-01 lounge memberships) and the approver levels
"manager" and "VP".  This module is the ladder those phrases refer to, ordered from
most to least senior.  Everything else reads it:

* ``users.grade`` stores a ``code`` from here (validated by the identity admin API;
  migration 0021 freezes the same list in a CHECK constraint, and
  ``backend/tests/test_kl_grade_ladder.py`` fails if the two drift apart);
* the rule engine compares ``rank`` against the AIR-CABIN rule's
  ``role_exception_role`` (``at_or_above``);
* the policy corpus (``database/seeds/corpus/policy.py`` DEF-09 / DEF-10 and the
  FAQ "Traveller Grades" section) renders the titles and entitlements from here, so
  RAG answers and the rule engine name the same ladder by construction.

Entitlements are ONLY what a corpus clause states.  The corpus names no per-grade
hotel cap, per-diem multiplier or rail entitlement, so none exist here.  Contractors
and other people without a grade on record have ``None`` (unknown): a grade-based
exception is then never granted automatically (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Grade:
    """One rung of the ladder.

    code   : stored in ``users.grade`` (upper-case, stable — never rename one).
    title  : the human title shown in the UI and the corpus.
    short  : the abbreviation the policy clauses use ("SVP", "VP").
    rank   : higher = more senior; "X and above" means rank >= X's rank.
    """

    code: str
    title: str
    short: str
    rank: int


# Most senior first.  Ranks are spaced so a future rung can be inserted without
# renumbering (a new rung still needs a migration: the CHECK constraint lists codes).
GRADES: Final[tuple[Grade, ...]] = (
    Grade("CSUITE", "Chief Officer", "C-suite", 80),
    Grade("SVP", "Senior Vice President", "SVP", 70),
    Grade("VP", "Vice President", "VP", 60),
    Grade("DIRECTOR", "Director", "Director", 50),
    Grade("MANAGER", "Manager", "Manager", 40),
    Grade("SENIOR_IC", "Senior Individual Contributor", "Senior IC", 30),
    Grade("IC", "Individual Contributor", "IC", 20),
    Grade("INTERN", "Intern", "Intern", 10),
)

GRADE_CODES: Final[tuple[str, ...]] = tuple(g.code for g in GRADES)
_BY_CODE: Final[dict[str, Grade]] = {g.code: g for g in GRADES}

# The threshold every "SVPs and above" clause names.
SENIOR_EXCEPTION_MIN_GRADE: Final[str] = "SVP"


@dataclass(frozen=True)
class Entitlement:
    """A travel entitlement a corpus clause grants from ``min_grade`` upwards.

    ``rule_enforced`` is True when the deterministic rule engine applies it: AIR-04
    through the AIR-CABIN rule; AIR-07 / LOD-05 / NR-01 through the
    ``grade_entitlement`` process rules (follow-up, grades —
    ``ai_agents/tools/grade_entitlements.py``).
    """

    clause_id: str
    min_grade: str
    summary: str
    rule_enforced: bool


# Every clause in TEP-FY26 that grants something by grade (all say "SVPs and above").
# AIR-04's hour threshold lives in the corpus figures (SVP_BUSINESS_MIN_HOURS) and in
# the AIR-CABIN rule definition (role_exception_min_hours); the corpus renders it.
ENTITLEMENTS: Final[tuple[Entitlement, ...]] = (
    Entitlement("AIR-04", SENIOR_EXCEPTION_MIN_GRADE, "business class on long flights", True),
    Entitlement("AIR-07", SENIOR_EXCEPTION_MIN_GRADE, "early-boarding and lounge fees", True),
    Entitlement("LOD-05", SENIOR_EXCEPTION_MIN_GRADE, "suites and upgrades", True),
    Entitlement("NR-01", SENIOR_EXCEPTION_MIN_GRADE, "lounge memberships", True),
)


def grade(code: str | None) -> Grade | None:
    """The ladder rung for *code* (exact code), or None when unknown / not a code."""
    if not isinstance(code, str):
        return None
    return _BY_CODE.get(code)


def normalize_grade(raw: str) -> str:
    """A ladder code from user input (code or title, any case / spacing).

    Raises ValueError for anything that is not on the ladder.
    """
    text = " ".join(str(raw).split())
    if not text:
        raise ValueError("empty grade")
    upper = text.upper().replace(" ", "_").replace("-", "_")
    if upper in _BY_CODE:
        return upper
    for g in GRADES:
        if text.casefold() in (g.title.casefold(), g.short.casefold()):
            return g.code
    raise ValueError(f"unknown grade {raw!r}")


def at_or_above(code: str | None, threshold: str | None) -> bool | None:
    """True when *code* is *threshold* or more senior; False when it is junior.

    None when either is not a ladder code — the caller must treat that as
    "could not verify" (never as entitled).
    """
    have, need = grade(code), grade(threshold)
    if have is None or need is None:
        return None
    return have.rank >= need.rank


def grades_at_or_above(threshold: str) -> tuple[Grade, ...]:
    """The rungs entitled by an "X and above" clause, most senior first."""
    need = grade(threshold)
    if need is None:
        raise ValueError(f"unknown grade {threshold!r}")
    return tuple(g for g in GRADES if g.rank >= need.rank)


def title_of(code: str | None) -> str | None:
    g = grade(code)
    return g.title if g else None


# Approver levels a flag may name (``requires_approver``) that are GRADES of the ladder
# (follow-up, grades: the SVP approval level).  "manager" is the submitter's line
# manager — a reporting relationship, not the Manager grade — and "Legal/Compliance"
# is a function; neither names a grade.
_NOT_A_GRADE_APPROVER: Final[frozenset[str]] = frozenset({"MANAGER"})


def approver_grade(requirement: str | None) -> str | None:
    """The ladder code an approver requirement names ("SVP", "VP", "Vice President",
    "C-suite"), or None when it names no grade ("manager", "Legal/Compliance", None)."""
    if not isinstance(requirement, str) or not requirement.strip():
        return None
    try:
        code = normalize_grade(requirement)
    except ValueError:
        return None
    return None if code in _NOT_A_GRADE_APPROVER else code


def most_senior(codes: list[str]) -> str | None:
    """The most senior ladder code of *codes* (unknown codes ignored), or None."""
    rungs = [g for c in codes if (g := grade(c)) is not None]
    return max(rungs, key=lambda g: g.rank).code if rungs else None
