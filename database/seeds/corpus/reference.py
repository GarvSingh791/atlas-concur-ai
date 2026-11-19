"""Helios Receipt & Documentation Quick Reference (document (d) of the corpus).

Original fresh-authored synthetic content for Helios Dynamics, Inc.  This is the
dense, scan-first companion to the authoritative policy (TEP-FY26): thresholds,
required documents per category, affidavit rules, deadlines, card reconciliation
timing, and the per-diem / caps quick tables.  Every figure is interpolated from
figures.py so the quick reference can never contradict the policy.

Exports a single module-level ``DOCUMENT: CorpusDocument`` with exactly 25
entries (REF-01 … REF-25).
"""

from __future__ import annotations

from database.seeds.corpus import figures as F
from database.seeds.corpus._types import CorpusDocument, CorpusUnit

_UNITS: tuple[CorpusUnit, ...] = (
    # --- Receipt thresholds -------------------------------------------------
    CorpusUnit(
        id="REF-01",
        section="Receipt Thresholds",
        text=(
            "REF-01 Receipt threshold: itemized receipt required for any "
            f"expense ≥ {F.usd(F.RECEIPT_THRESHOLD)}; airfare and hotel folios "
            "always require a receipt regardless of amount (EXP-02)."
        ),
    ),
    CorpusUnit(
        id="REF-02",
        section="Receipt Thresholds",
        text=(
            f"REF-02 Below {F.usd(F.RECEIPT_THRESHOLD)}: itemized receipt not "
            "mandatory, but date, amount, vendor, and business purpose are "
            "still required on the line (EXP-03)."
        ),
    ),
    CorpusUnit(
        id="REF-03",
        section="Receipt Thresholds",
        text=(
            "REF-03 Itemized vs card slip: a card slip alone is insufficient at "
            f"or above {F.usd(F.RECEIPT_THRESHOLD)}; the receipt must show line "
            "items (EXP-02)."
        ),
    ),
    # --- Required documentation per category --------------------------------
    CorpusUnit(
        id="REF-04",
        section="Required Documentation",
        text=(
            "REF-04 Airfare docs: e-ticket / itinerary receipt always required; "
            "fare class and routing visible. Change fees need a business reason "
            "(EXP-03, AIR-08)."
        ),
    ),
    CorpusUnit(
        id="REF-05",
        section="Required Documentation",
        text=(
            "REF-05 Hotel docs: full folio (room + tax + incidentals) always "
            "required regardless of amount; itemize personal incidentals out "
            "(EXP-02, LOD-04)."
        ),
    ),
    CorpusUnit(
        id="REF-06",
        section="Required Documentation",
        text=(
            "REF-06 Meals / per diem docs: per diem needs travel dates and "
            "locations, no meal receipts; itemized meals need a receipt at or "
            f"above {F.usd(F.RECEIPT_THRESHOLD)} (EXP-05, EXP-02, MEAL-03)."
        ),
    ),
    CorpusUnit(
        id="REF-07",
        section="Required Documentation",
        text=(
            "REF-07 Ground transport docs: rideshare/taxi/rail receipt; tolls "
            "and tips itemized on the ride. Premium tiers need a safety or "
            "group justification (EXP-03, GRND-02, GRND-05)."
        ),
    ),
    CorpusUnit(
        id="REF-08",
        section="Required Documentation",
        text=(
            "REF-08 Mileage docs: start/end addresses, business miles, commute "
            "offset; no fuel receipt for personal-vehicle mileage (EXP-03, "
            "MIL-01, MIL-02)."
        ),
    ),
    CorpusUnit(
        id="REF-09",
        section="Required Documentation",
        text=(
            "REF-09 Entertainment docs: receipt plus all attendee names and "
            "companies and the business purpose; alcohol only within approved "
            "client entertainment (EXP-03, ENT-01, ENT-03)."
        ),
    ),
    # --- Affidavits ---------------------------------------------------------
    CorpusUnit(
        id="REF-10",
        section="Affidavits",
        text=(
            "REF-10 Missing-receipt affidavit: allowed only for amounts ≤ "
            f"{F.usd(F.AFFIDAVIT_MAX_AMOUNT)}, no more than "
            f"{F.AFFIDAVIT_MAX_PER_QUARTER}× per quarter; signed certification "
            "replaces the receipt (EXP-04)."
        ),
    ),
    CorpusUnit(
        id="REF-11",
        section="Affidavits",
        text=(
            "REF-11 Affidavit not allowed: amounts above "
            f"{F.usd(F.AFFIDAVIT_MAX_AMOUNT)}, airfare, and hotel folios — a "
            "real receipt is required for these (EXP-04, EXP-02)."
        ),
    ),
    # --- Deadlines ----------------------------------------------------------
    CorpusUnit(
        id="REF-12",
        section="Deadlines",
        text=(
            "REF-12 Submission deadline: submit reports within "
            f"{F.SUBMISSION_DEADLINE_DAYS} days of trip completion (EXP-01)."
        ),
    ),
    CorpusUnit(
        id="REF-13",
        section="Deadlines",
        text=(
            f"REF-13 Stale reports: older than {F.STALE_REPORT_DAYS} days "
            "require VP approval and may be treated as taxable income "
            "(EXP-01)."
        ),
    ),
    CorpusUnit(
        id="REF-14",
        section="Deadlines",
        text=(
            f"REF-14 Reimbursement target: {F.REIMBURSEMENT_DAYS} business days "
            "after final approval via direct deposit; resubmission restarts the "
            "clock (REIMB-01, REIMB-02)."
        ),
    ),
    CorpusUnit(
        id="REF-15",
        section="Deadlines",
        text=(
            "REF-15 Advance booking: domestic flights ≥ "
            f"{F.ADVANCE_BOOKING_DOMESTIC_DAYS} days ahead, international ≥ "
            f"{F.ADVANCE_BOOKING_INTL_DAYS} days ahead (PTA-02)."
        ),
    ),
    # --- Corporate card -----------------------------------------------------
    CorpusUnit(
        id="REF-16",
        section="Corporate Card",
        text=(
            "REF-16 Card reconciliation: reconcile all card transactions in a "
            f"report within {F.CARD_RECONCILE_DEADLINE_DAYS} days, inside the "
            "EXP-01 window (CARD-03)."
        ),
    ),
    CorpusUnit(
        id="REF-17",
        section="Corporate Card",
        text=(
            "REF-17 Lost/stolen card: report to the issuer and Finance within "
            f"{F.CARD_LOSS_REPORT_HOURS}h; cash advances are not permitted "
            "(CARD-04, CARD-05)."
        ),
    ),
    CorpusUnit(
        id="REF-18",
        section="Corporate Card",
        text=(
            "REF-18 Card usage: business expenses only; accidental personal "
            "charges must be repaid immediately via 'Owe Company' (CARD-01, "
            "CARD-02)."
        ),
    ),
    # --- Per diem quick table ----------------------------------------------
    CorpusUnit(
        id="REF-19",
        section="Per Diem Quick Table",
        text=(
            "REF-19 Per diem (meals & incidentals) per day by city tier: "
            f"Tier 1 {F.usd(F.PER_DIEM['tier1'])}, "
            f"Tier 2 {F.usd(F.PER_DIEM['tier2'])}, "
            f"Tier 3 {F.usd(F.PER_DIEM['tier3'])} (MEAL-01)."
        ),
    ),
    CorpusUnit(
        id="REF-20",
        section="Per Diem Quick Table",
        text=(
            f"REF-20 Partial days: {F.PER_DIEM_PARTIAL_PCT}% of per diem on "
            "departure and return days; per diem is reduced for any "
            "host/conference-provided meal (MEAL-02, MEAL-06)."
        ),
    ),
    # --- Caps quick table ---------------------------------------------------
    CorpusUnit(
        id="REF-21",
        section="Caps Quick Table",
        text=(
            "REF-21 Lodging nightly cap (room + tax) by city tier: "
            f"Tier 1 {F.usd(F.LODGING_CAP['tier1'])}, "
            f"Tier 2 {F.usd(F.LODGING_CAP['tier2'])}, "
            f"Tier 3 {F.usd(F.LODGING_CAP['tier3'])} per night (LOD-01)."
        ),
    ),
    CorpusUnit(
        id="REF-22",
        section="Caps Quick Table",
        text=(
            f"REF-22 Lodging exception: ≤ {F.LODGING_CAP_AUTO_EXCEPTION_PCT}% "
            "over cap auto-approved through the tool; above that requires "
            "manager approval with justification (LOD-02)."
        ),
    ),
    CorpusUnit(
        id="REF-23",
        section="Caps Quick Table",
        text=(
            "REF-23 Client meal cap per head by city tier: "
            f"Tier 1 {F.usd(F.CLIENT_MEAL_CAP['tier1'])}, "
            f"Tier 2 {F.usd(F.CLIENT_MEAL_CAP['tier2'])}, "
            f"Tier 3 {F.usd(F.CLIENT_MEAL_CAP['tier3'])} (ENT-02). "
            f"Gifts over {F.usd(F.GIFT_DISCLOSURE_THRESHOLD)} require "
            "disclosure (ENT-04)."
        ),
    ),
    CorpusUnit(
        id="REF-24",
        section="Caps Quick Table",
        text=(
            "REF-24 Team dinner cap per head by city tier: "
            f"Tier 1 {F.usd(F.TEAM_DINNER_CAP['tier1'])}, "
            f"Tier 2 {F.usd(F.TEAM_DINNER_CAP['tier2'])}, "
            f"Tier 3 {F.usd(F.TEAM_DINNER_CAP['tier3'])} per head; senior host "
            "pays and lists attendees (MEAL-05)."
        ),
    ),
    # --- Audit / retention --------------------------------------------------
    CorpusUnit(
        id="REF-25",
        section="Deadlines",
        text=(
            "REF-25 Audit retention: all expense reports are subject to audit "
            f"and retained for {F.AUDIT_RETENTION_YEARS} years (POL-04, "
            "DATA-01)."
        ),
    ),
)

DOCUMENT = CorpusDocument(
    doc_id="REF-FY26",
    document_type="reference",
    title="Helios Receipt & Documentation Quick Reference",
    version=1,
    effective_from=F.EFFECTIVE_FROM,
    header=(
        "HELIOS RECEIPT & DOCUMENTATION — QUICK REFERENCE\n"
        f"{F.COMPANY} · Companion to {F.POLICY_DOC_ID}"
    ),
    units=_UNITS,
)
