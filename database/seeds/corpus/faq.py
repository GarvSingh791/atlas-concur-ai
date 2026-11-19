"""Helios Dynamics T&E FAQ — document (b) of the Atlas RAG corpus (R0-T2).

Original, fresh-authored conversational Q&A whose answers restate the canonical
policy rules (TEP-FY26) and cite the governing clause id.  Every numeric answer
interpolates from ``figures.py`` so the FAQ can never contradict the policy.

Exports a single module-level ``DOCUMENT: CorpusDocument`` of 59 Q&A units
(FAQ-Q01 … FAQ-Q59; Q51–Q59 = traveler grades, known limits AIR-04).
"""

from __future__ import annotations

from database.seeds.corpus import figures as F
from database.seeds.corpus import grades as _GR
from database.seeds.corpus._types import CorpusDocument, CorpusUnit

_UNITS: tuple[CorpusUnit, ...] = (
    # ------------------------------------------------------------------ Air Travel
    CorpusUnit(
        id="FAQ-Q01",
        section="Air Travel",
        text=(
            "Q: What cabin can I book for a 12-hour flight?\n"
            f"A: Flights longer than {F.CABIN_BUSINESS_MIN_HOURS} hours may be booked in "
            "business class with VP approval (first class is never reimbursable); "
            f"economy is the standard for flights under {F.CABIN_ECONOMY_MAX_HOURS} hours "
            "(see AIR-03)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q02",
        section="Air Travel",
        text=(
            "Q: Can I fly economy on a short domestic hop?\n"
            f"A: Yes — economy is required for all flights under {F.CABIN_ECONOMY_MAX_HOURS} "
            "hours of scheduled flight time (see AIR-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q03",
        section="Air Travel",
        text=(
            "Q: When can I use premium economy?\n"
            f"A: Premium economy is permitted on flights of {F.CABIN_PREMIUM_MIN_HOURS}–"
            f"{F.CABIN_PREMIUM_MAX_HOURS} hours with manager approval (see AIR-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q04",
        section="Air Travel",
        text=(
            "Q: I'm an SVP — do the cabin rules differ for me?\n"
            f"A: Yes — SVPs and above may book business class on flights longer than "
            f"{F.SVP_BUSINESS_MIN_HOURS} hours (see AIR-04)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q05",
        section="Air Travel",
        text=(
            "Q: Do I have to take the cheapest flight even if it's inconvenient?\n"
            f"A: You must accept fares within {F.usd(F.LOWEST_FARE_TOLERANCE_DOMESTIC)} "
            f"(domestic) or {F.usd(F.LOWEST_FARE_TOLERANCE_INTL)} (international) of the "
            "lowest logical fare meeting your routing and timing needs (see AIR-06)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q06",
        section="Air Travel",
        text=(
            "Q: Should I book a preferred airline if it costs a bit more?\n"
            f"A: Book a Helios preferred carrier when it is within {F.PREFERRED_FARE_PCT}% "
            "of the lowest logical fare (see AIR-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q07",
        section="Air Travel",
        text=(
            "Q: Are baggage and seat fees reimbursable?\n"
            "A: One checked bag, standard seat selection, and Wi-Fi are reimbursable; "
            f"extra-legroom seats only on flights over {F.CABIN_ECONOMY_MAX_HOURS} hours, "
            "and lounge/early-boarding fees are not (see AIR-07)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q08",
        section="Air Travel",
        text=(
            "Q: My meeting got moved and I need to change my flight — is the fee covered?\n"
            "A: Change fees for business-driven changes are reimbursable; route the change "
            "through the TMC to preserve ticket value and report any unused tickets "
            "(see AIR-08)."
        ),
    ),
    # ----------------------------------------------------- Approvals & Booking
    CorpusUnit(
        id="FAQ-Q09",
        section="Approvals & Booking",
        text=(
            "Q: How far ahead must I book my trip?\n"
            f"A: Book domestic flights at least {F.ADVANCE_BOOKING_DOMESTIC_DAYS} days "
            f"ahead and international flights at least {F.ADVANCE_BOOKING_INTL_DAYS} days "
            "ahead; shorter lead time needs a documented business reason (see PTA-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q10",
        section="Approvals & Booking",
        text=(
            "Q: Do I need approval before I book anything?\n"
            "A: Yes — every trip requires manager Pre-Trip Approval recorded in the travel "
            "tool before booking (see PTA-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q11",
        section="Approvals & Booking",
        text=(
            "Q: My trip cost went up after I rebooked — do I need re-approval?\n"
            f"A: Trip changes must be re-approved if the total cost increases by more than "
            f"{F.TRIP_CHANGE_REAPPROVAL_PCT}% (see PTA-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q12",
        section="Approvals & Booking",
        text=(
            "Q: Can I just book outside the company tool if I find a better deal myself?\n"
            "A: No — out-of-channel ('leakage') bookings are reimbursed only with an "
            "approved exception; use the Online Booking Tool or TMC (see PRIN-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q13",
        section="Approvals & Booking",
        text=(
            "Q: Can I add a few personal days to a business trip?\n"
            "A: Bleisure is allowed if it creates no extra cost to the company; you pay any "
            "fare or room difference (see PRIN-03)."
        ),
    ),
    # ------------------------------------------------------- Hotels & Lodging
    CorpusUnit(
        id="FAQ-Q14",
        section="Hotels & Lodging",
        text=(
            "Q: What's the hotel cap in NYC or London?\n"
            f"A: New York and London are Tier 1, so the nightly cap (room + tax) is "
            f"{F.usd(F.LODGING_CAP['tier1'])}/night (see LOD-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q15",
        section="Hotels & Lodging",
        text=(
            "Q: What's the hotel cap in a Tier 2 city like Chicago?\n"
            f"A: Tier 2 cities are capped at {F.usd(F.LODGING_CAP['tier2'])}/night "
            "(room + tax); standard Tier 3 locations are capped at "
            f"{F.usd(F.LODGING_CAP['tier3'])}/night (see LOD-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q16",
        section="Hotels & Lodging",
        text=(
            "Q: The conference hotel is a little over the cap — is that OK?\n"
            f"A: Rates exceeding the cap by up to {F.LODGING_CAP_AUTO_EXCEPTION_PCT}% are "
            "auto-approved when booked through the tool; more than that needs manager "
            "approval with justification (see LOD-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q17",
        section="Hotels & Lodging",
        text=(
            "Q: Can I expense the minibar and an in-room movie?\n"
            "A: No — minibar, movies, spa, and gym fees are not reimbursable; in-room "
            "Wi-Fi, one daily breakfast, and reasonable laundry on long trips are "
            "(see LOD-04)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q18",
        section="Hotels & Lodging",
        text=(
            "Q: Can I book an Airbnb instead of a hotel?\n"
            "A: Yes, if the total cost is at or below the applicable hotel cap and you "
            "verify safety and business-suitability (see LOD-06)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q19",
        section="Hotels & Lodging",
        text=(
            "Q: Can I book a suite or a room upgrade?\n"
            "A: No — standard rooms only; suites and upgrades are not reimbursable except "
            "for SVPs and above (see LOD-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q20",
        section="Hotels & Lodging",
        text=(
            "Q: I got charged a late-cancellation fee — is it covered?\n"
            "A: No-show and late-cancellation charges are reimbursable only for business "
            "reasons; otherwise they are personal (see LOD-07)."
        ),
    ),
    # ------------------------------------------------------ Meals & Per Diem
    CorpusUnit(
        id="FAQ-Q21",
        section="Meals & Per Diem",
        text=(
            "Q: What's the per diem in New York?\n"
            f"A: New York is a Tier 1 city, so the meals & incidentals per diem is "
            f"{F.usd(F.PER_DIEM['tier1'])}/day (see MEAL-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q22",
        section="Meals & Per Diem",
        text=(
            "Q: What's the per diem for a Tier 2 city?\n"
            f"A: Tier 2 cities have a per diem of {F.usd(F.PER_DIEM['tier2'])}/day "
            "(see MEAL-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q23",
        section="Meals & Per Diem",
        text=(
            "Q: What's the per diem for a standard (Tier 3) location?\n"
            f"A: Standard Tier 3 locations have a per diem of {F.usd(F.PER_DIEM['tier3'])}"
            "/day (see MEAL-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q24",
        section="Meals & Per Diem",
        text=(
            "Q: How much per diem do I get on my travel day?\n"
            f"A: Departure and return days are paid at {F.PER_DIEM_PARTIAL_PCT}% of the "
            "per diem (see MEAL-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q25",
        section="Meals & Per Diem",
        text=(
            "Q: Can I expense alcohol at a team dinner?\n"
            "A: No — personal alcohol is not reimbursable under per diem or individual "
            "meals (see MEAL-04); alcohol is reimbursable only as part of approved client "
            "entertainment (see ENT-03)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q26",
        section="Meals & Per Diem",
        text=(
            "Q: Who pays at a team dinner, and is there a cap?\n"
            "A: The most senior employee present pays and expenses, listing all attendees; "
            f"per-head dinner caps are {F.usd(F.TEAM_DINNER_CAP['tier1'])} (Tier 1), "
            f"{F.usd(F.TEAM_DINNER_CAP['tier2'])} (Tier 2), and "
            f"{F.usd(F.TEAM_DINNER_CAP['tier3'])} (Tier 3) (see MEAL-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q27",
        section="Meals & Per Diem",
        text=(
            "Q: Do I submit meal receipts, or just claim per diem?\n"
            "A: The company uses per diem by default; itemized meal reimbursement is "
            "allowed only where local tax law requires it, capped at the per diem amount "
            "(see MEAL-03)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q28",
        section="Meals & Per Diem",
        text=(
            "Q: Breakfast was included at my conference — does that affect per diem?\n"
            "A: Yes — per diem is reduced for any meal provided by a conference, airline, "
            "or host (see MEAL-06)."
        ),
    ),
    # ------------------------------------------------- Receipts & Submission
    CorpusUnit(
        id="FAQ-Q29",
        section="Receipts & Submission",
        text=(
            "Q: How long do I have to submit my expenses?\n"
            f"A: Submit expense reports within {F.SUBMISSION_DEADLINE_DAYS} days of trip "
            f"completion; reports older than {F.STALE_REPORT_DAYS} days require VP approval "
            "and may be treated as taxable (see EXP-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q30",
        section="Receipts & Submission",
        text=(
            "Q: When do I need a receipt?\n"
            f"A: An itemized receipt is required for any expense of "
            f"{F.usd(F.RECEIPT_THRESHOLD)} or more; hotel folios and airfare always "
            "require a receipt regardless of amount (see EXP-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q31",
        section="Receipts & Submission",
        text=(
            "Q: I lost a receipt — what now?\n"
            f"A: You may file a signed missing-receipt affidavit up to "
            f"{F.usd(F.AFFIDAVIT_MAX_AMOUNT)}, no more than "
            f"{F.AFFIDAVIT_MAX_PER_QUARTER} times per quarter (see EXP-04)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q32",
        section="Receipts & Submission",
        text=(
            "Q: What details does each expense line need?\n"
            "A: Each expense needs the date, amount, vendor, business purpose, and "
            "attendees for meals or entertainment (see EXP-03)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q33",
        section="Receipts & Submission",
        text=(
            "Q: Do I need receipts for per diem days?\n"
            "A: No — per diem claims do not require meal receipts, but you must record the "
            "travel dates and locations (see EXP-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q34",
        section="Receipts & Submission",
        text=(
            "Q: Who's responsible if my report has an error?\n"
            "A: You certify the report is accurate, and managers are accountable for what "
            "they approve (see EXP-06)."
        ),
    ),
    # ------------------------------------------------------- Reimbursement
    CorpusUnit(
        id="FAQ-Q35",
        section="Reimbursement",
        text=(
            "Q: When will I be reimbursed?\n"
            f"A: Target reimbursement is within {F.REIMBURSEMENT_DAYS} business days of "
            "final approval, paid by direct deposit (see REIMB-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q36",
        section="Reimbursement",
        text=(
            "Q: An item on my report was flagged — what happens?\n"
            "A: Disputed or flagged items are returned with a reason; corrected "
            "resubmission restarts the reimbursement clock (see REIMB-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q37",
        section="Reimbursement",
        text=(
            "Q: Will I be paid back for charges already on my corporate card?\n"
            "A: No — corporate-card expenses are paid directly with no personal "
            "reimbursement; only personal-card expenses are reimbursed to you "
            "(see REIMB-03)."
        ),
    ),
    # ------------------------------------------------------- Corporate Card
    CorpusUnit(
        id="FAQ-Q38",
        section="Corporate Card",
        text=(
            "Q: I lost my corporate card — what do I do?\n"
            f"A: Report it to the issuer and Finance within {F.CARD_LOSS_REPORT_HOURS} "
            "hours (see CARD-04)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q39",
        section="Corporate Card",
        text=(
            "Q: I accidentally used my corporate card for a personal charge.\n"
            "A: Personal use is prohibited; repay any accidental personal charge "
            "immediately (see CARD-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q40",
        section="Corporate Card",
        text=(
            "Q: How soon must I reconcile my corporate-card charges?\n"
            f"A: Reconcile all card transactions in an expense report within the "
            f"{F.CARD_RECONCILE_DEADLINE_DAYS}-day submission window (see CARD-03)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q41",
        section="Corporate Card",
        text=(
            "Q: Can I take a cash advance on the corporate card?\n"
            "A: No — cash advances on the corporate card are not permitted (see CARD-05)."
        ),
    ),
    # ------------------------------------------------------- Ground & Rail
    CorpusUnit(
        id="FAQ-Q42",
        section="Ground & Rail",
        text=(
            "Q: Can I book first-class rail?\n"
            f"A: First/business rail is permitted for trips of {F.RAIL_FIRST_MIN_HOURS} "
            "hours or more when it is cheaper than or comparable to flying; otherwise book "
            "standard class (see GRND-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q43",
        section="Ground & Rail",
        text=(
            "Q: How long can I leave my car at the airport?\n"
            f"A: Airport parking is reimbursable up to {F.AIRPORT_PARKING_MAX_DAYS} days in "
            "the economy lot (see GRND-03)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q44",
        section="Ground & Rail",
        text=(
            "Q: How much can I tip and still get reimbursed?\n"
            f"A: Reasonable tips up to {F.TIP_MAX_PCT}%, plus tolls, are reimbursable with "
            "the ride (see GRND-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q45",
        section="Ground & Rail",
        text=(
            "Q: Can I take Uber Black or a premium rideshare?\n"
            "A: No — use the standard tier (UberX/Lyft, standard taxi); premium tiers are "
            "not reimbursable except for safety or group needs (see GRND-02)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q46",
        section="Ground & Rail",
        text=(
            "Q: I'd rather drive my own car the whole way — is that capped?\n"
            f"A: For trips over {F.MILEAGE_AIR_CAP_MILES} miles each way, mileage is capped "
            "at the equivalent lowest logical airfare unless flying was impractical "
            "(see MIL-03)."
        ),
    ),
    # --------------------------------------------------------- Rental Cars
    CorpusUnit(
        id="FAQ-Q47",
        section="Rental Cars",
        text=(
            "Q: What class of rental car can I book?\n"
            "A: Economy/intermediate (compact/midsize); full-size is allowed for 3+ "
            "travelers or significant equipment, and SUV/premium needs manager approval "
            "(see CAR-01)."
        ),
    ),
    # ------------------------------------------------------- International
    CorpusUnit(
        id="FAQ-Q48",
        section="International",
        text=(
            "Q: Are visa and roaming costs covered for an overseas trip?\n"
            "A: Business visa and entry-requirement costs are reimbursable (see INTL-01), "
            "and an international roaming plan or local eSIM is reimbursable while personal "
            "overages are not (see INTL-03)."
        ),
    ),
    # -------------------------------------------------------- Entertainment
    CorpusUnit(
        id="FAQ-Q49",
        section="Entertainment",
        text=(
            "Q: How much can I spend taking a client to dinner?\n"
            f"A: Client meals are capped at {F.usd(F.CLIENT_MEAL_CAP['tier1'])}/head "
            f"(Tier 1), {F.usd(F.CLIENT_MEAL_CAP['tier2'])} (Tier 2), and "
            f"{F.usd(F.CLIENT_MEAL_CAP['tier3'])} (Tier 3), with all attendees documented; "
            "larger events need pre-approval (see ENT-02)."
        ),
    ),
    # ------------------------------------------------------------- General
    CorpusUnit(
        id="FAQ-Q50",
        section="General",
        text=(
            "Q: I'm not sure if something is allowed — what should I do?\n"
            "A: When in doubt, seek approval before you spend; the company reimburses only "
            "actual, reasonable business expenses (see PRIN-04)."
        ),
    ),
    # ------------------------------------------------------- Traveler Grades
    # Rendered from the one grade ladder (config/constants/grades.py via
    # corpus/grades.py) and figures.py, so these answers match the AIR-04 rule.
    CorpusUnit(
        id="FAQ-Q51",
        section="Traveler Grades",
        text=(
            "Q: What traveler grades does the travel policy use?\n"
            f"A: From most senior: {_GR.ladder_text()} (see DEF-09). Your grade of record "
            "decides the grade-based exceptions (see DEF-10)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q52",
        section="Traveler Grades",
        text=(
            f'Q: Who counts as "{_GR.SVP_SHORT}s and above"?\n'
            f"A: {_GR.senior_grades_text()} — every grade at or above Senior Vice "
            f"President on the grade ladder (see DEF-09). They may book business class on "
            f"flights longer than {F.SVP_BUSINESS_MIN_HOURS} hours (see AIR-04)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q53",
        section="Traveler Grades",
        text=(
            "Q: I'm a Vice President — does the SVP exception apply to me?\n"
            f"A: No — AIR-04 covers {_GR.SVP_SHORT}s and above only, so a Vice President "
            "follows the standard cabin tiers: premium economy on "
            f"{F.CABIN_PREMIUM_MIN_HOURS}–{F.CABIN_PREMIUM_MAX_HOURS} hours with manager "
            "approval (see AIR-02) and business only under AIR-03; anything else needs an "
            "approved exception (see POL-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q54",
        section="Traveler Grades",
        text=(
            f"Q: Does the {_GR.SVP_SHORT} exception cover shorter trips too?\n"
            f"A: No — AIR-04 starts above {F.SVP_BUSINESS_MIN_HOURS} hours of scheduled "
            f"time; under {F.CABIN_ECONOMY_MAX_HOURS} hours economy applies to every grade "
            "(see AIR-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q55",
        section="Traveler Grades",
        text=(
            "Q: Does my grade change my nightly lodging cap or my per diem?\n"
            "A: No — lodging caps (see LOD-01) and meal per diems (see MEAL-01) are the "
            f"same for every grade. {_GR.SVP_SHORT}s and above differ only in the "
            "entitlements listed in DEF-10, such as suites and upgrades (see LOD-05)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q56",
        section="Traveler Grades",
        text=(
            "Q: Who maintains my traveler grade, and can I change it myself?\n"
            "A: Finance keeps your grade of record; travelers cannot set or declare their "
            "own grade. Ask a finance administrator to correct it (see DEF-10)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q57",
        section="Traveler Grades",
        text=(
            "Q: What happens to a business-class booking if no grade is recorded for me?\n"
            "A: No grade-based exception applies automatically, so the booking goes to an "
            f"approver, who confirms whether the {_GR.SVP_SHORT}-and-above exception "
            "applies (see DEF-10 and AIR-04)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q58",
        section="Traveler Grades",
        text=(
            "Q: Can I expense airport lounge access or early boarding?\n"
            f"A: Only if you are an {_GR.SVP_SHORT} or above, or your flight was delayed "
            f"more than {F.LOUNGE_DELAY_EXCEPTION_HOURS} hours (see AIR-07); lounge "
            "memberships are reimbursable for "
            f"{_GR.SVP_SHORT}s and above only (see NR-01)."
        ),
    ),
    CorpusUnit(
        id="FAQ-Q59",
        section="Traveler Grades",
        text=(
            "Q: Do interns follow the same travel rules as everyone else?\n"
            "A: Yes — the policy applies to interns (see GEN-02). Intern is the most junior "
            "grade, so no grade-based exception applies (see DEF-10)."
        ),
    ),
)

DOCUMENT = CorpusDocument(
    doc_id="FAQ-FY26",
    document_type="faq",
    title="Helios T&E FAQ",
    version=1,
    effective_from=F.EFFECTIVE_FROM,
    header=(
        f"HELIOS T&E — FREQUENTLY ASKED QUESTIONS\n" f"{F.COMPANY} · Companion to {F.POLICY_DOC_ID}"
    ),
    units=_UNITS,
)
