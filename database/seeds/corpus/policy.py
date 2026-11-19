"""Document (a) of the Helios Dynamics T&E corpus: the policy (TEP-FY26).

The full Global Travel & Expense Policy, ported from the authoritative source
(Part 4 of the companion RAG research report) as original synthetic content.
Every dollar / day / percent / hour figure that has a constant in ``figures``
is interpolated from ``F`` (never hardcoded) so the rendered ``document_text``
agrees with the single source of truth by construction.
"""

from __future__ import annotations

from database.seeds.corpus import figures as F
from database.seeds.corpus import grades as _GR
from database.seeds.corpus._types import CorpusDocument, CorpusUnit

_S1 = "Section 1 — Purpose & Scope"
_S2 = "Section 2 — Definitions & Roles"
_S3 = "Section 3 — General Principles"
_S4 = "Section 4 — Pre-Trip Approval & Booking"
_S5 = "Section 5 — Air Travel"
_S6 = "Section 6 — Rail & Ground Transport"
_S7 = "Section 7 — Rental Cars"
_S8 = "Section 8 — Personal Vehicle & Mileage"
_S9 = "Section 9 — Hotels & Lodging"
_S10 = "Section 10 — Meals & Per Diem"
_S11 = "Section 11 — International Travel"
_S12 = "Section 12 — Entertainment & Client Hospitality"
_S13 = "Section 13 — Conferences, Events & Training"
_S14 = "Section 14 — Expense Submission & Documentation"
_S15 = "Section 15 — Reimbursement Process & Timelines"
_S16 = "Section 16 — Corporate Card Policy"
_S17 = "Section 17 — Non-Reimbursable Expenses"
_S18 = "Section 18 — Exceptions, Violations & Consequences"
_S19 = "Section 19 — Sustainability & Carbon"
_S20 = "Section 20 — Data, Privacy & Contacts"

_TIER1 = ", ".join(F.TIER1_CITIES)
_TIER2 = ", ".join(F.TIER2_CITIES)

_UNITS: tuple[CorpusUnit, ...] = (
    # ---- Section 1 — Purpose & Scope -------------------------------------
    CorpusUnit(
        id="GEN-01",
        section=_S1,
        text=(
            "GEN-01 Purpose: governs all business travel and reimbursable "
            "expenses for Helios Dynamics, balancing employee well-being and "
            "productivity against cost control and duty of care."
        ),
    ),
    CorpusUnit(
        id="GEN-02",
        section=_S1,
        text=(
            "GEN-02 Scope: applies to all employees, contractors, interns, and "
            "board members incurring business expenses globally. Where local "
            "law or a collective agreement is stricter, the stricter rule "
            "applies."
        ),
    ),
    CorpusUnit(
        id="GEN-03",
        section=_S1,
        text=(
            "GEN-03 Guiding principle: spend the company's money as if it were "
            "your own — choose the most reasonable, cost-effective option that "
            "still lets you do good work. Every expense must be "
            "business-necessary, properly documented, and within policy."
        ),
    ),
    CorpusUnit(
        id="GEN-04",
        section=_S1,
        text=(
            "GEN-04 Compliance philosophy: policy rules are enforced in the "
            "booking and expense tools; compliant behavior should be the easy "
            "default. Repeated or deliberate violations have consequences "
            "(Section 18)."
        ),
    ),
    CorpusUnit(
        id="GEN-05",
        section=_S1,
        text=(
            f"GEN-05 Effective date & precedence: {F.POLICY_DOC_ID} supersedes "
            f"all prior T&E policies as of {F.EFFECTIVE_FROM}."
        ),
    ),
    # ---- Section 2 — Definitions & Roles ---------------------------------
    CorpusUnit(
        id="DEF-01",
        section=_S2,
        text="DEF-01 Traveler: any person traveling on approved company business.",
    ),
    CorpusUnit(
        id="DEF-02",
        section=_S2,
        text=(
            "DEF-02 Approver / Manager: the traveler's direct manager (or "
            "budget owner) who authorizes trips and approves expense reports."
        ),
    ),
    CorpusUnit(
        id="DEF-03",
        section=_S2,
        text=(
            "DEF-03 Travel Desk: internal team plus designated TMC (Travel "
            "Management Company) handling bookings and emergencies "
            f"({F.TRAVEL_DESK_EMAIL}, 24/7 line {F.TRAVEL_DESK_PHONE})."
        ),
    ),
    CorpusUnit(
        id="DEF-04",
        section=_S2,
        text=("DEF-04 Finance / AP: reviews, audits, and reimburses approved " "expense reports."),
    ),
    CorpusUnit(
        id="DEF-05",
        section=_S2,
        text=(
            f"DEF-05 City tiers: Tier 1 (Premium): {_TIER1}. "
            f"Tier 2 (High-cost): {_TIER2}. "
            "Tier 3 (Standard): all other locations."
        ),
    ),
    CorpusUnit(
        id="DEF-06",
        section=_S2,
        text=(
            "DEF-06 Per diem: a fixed daily allowance for meals & incidentals "
            "in lieu of itemized receipts, benchmarked to GSA (US) / ECA "
            "(international) rates."
        ),
    ),
    CorpusUnit(
        id="DEF-07",
        section=_S2,
        text=(
            "DEF-07 Preferred supplier: a vendor (airline, hotel chain, car "
            "company) with a negotiated Helios corporate rate; use when "
            "reasonably available."
        ),
    ),
    CorpusUnit(
        id="DEF-08",
        section=_S2,
        text=(
            "DEF-08 Pre-Trip Approval (PTA): written manager approval recorded "
            "in the travel tool before booking."
        ),
    ),
    CorpusUnit(
        id="DEF-09",
        section=_S2,
        text=(
            f"DEF-09 Traveler grades, most senior first: {_GR.ladder_text()}. "
            f'"{_GR.SVP_SHORT}s and above" means {_GR.senior_grades_text()}.'
        ),
    ),
    CorpusUnit(
        id="DEF-10",
        section=_S2,
        text=(
            "DEF-10 Grade entitlements: grade-based exceptions follow the traveler's "
            "grade of record, which Finance maintains; travelers cannot set or declare "
            f"their own grade. {_GR.SVP_SHORT}s and above may claim: "
            f"{_GR.entitlements_text()}. {_GR.other_grades_text()} follow the standard "
            "rules. With no grade on record, no grade-based exception applies "
            "automatically and the approver decides."
        ),
    ),
    # ---- Section 3 — General Principles -----------------------------------
    CorpusUnit(
        id="PRIN-01",
        section=_S3,
        text=(
            "PRIN-01 Book through the designated Online Booking Tool / TMC; "
            "out-of-channel bookings ('leakage') are reimbursed only with an "
            "approved exception."
        ),
    ),
    CorpusUnit(
        id="PRIN-02",
        section=_S3,
        text=(
            "PRIN-02 The company reimburses actual, reasonable business "
            "expenses; personal portions are never reimbursable."
        ),
    ),
    CorpusUnit(
        id="PRIN-03",
        section=_S3,
        text=(
            "PRIN-03 Combining business and personal travel ('bleisure') is "
            "allowed if it creates no extra cost to the company; the traveler "
            "pays any fare/room difference."
        ),
    ),
    CorpusUnit(
        id="PRIN-04",
        section=_S3,
        text="PRIN-04 When in doubt, seek approval before spending.",
    ),
    # ---- Section 4 — Pre-Trip Approval & Booking --------------------------
    CorpusUnit(
        id="PTA-01",
        section=_S4,
        text=("PTA-01 All trips require manager Pre-Trip Approval before booking."),
    ),
    CorpusUnit(
        id="PTA-02",
        section=_S4,
        text=(
            "PTA-02 Advance booking: domestic flights "
            f"≥ {F.ADVANCE_BOOKING_DOMESTIC_DAYS} days ahead; international "
            f"≥ {F.ADVANCE_BOOKING_INTL_DAYS} days ahead, to capture "
            "advance-purchase fares. Shorter lead time requires a documented "
            "business reason."
        ),
    ),
    CorpusUnit(
        id="PTA-03",
        section=_S4,
        text=(
            "PTA-03 Booking channel: Online Booking Tool first; "
            "complex/multi-leg itineraries via the TMC agent."
        ),
    ),
    CorpusUnit(
        id="PTA-04",
        section=_S4,
        text=(
            "PTA-04 International / high-risk trips require additional approval "
            "from the department VP and a Travel Desk security review "
            "(Section 11)."
        ),
    ),
    CorpusUnit(
        id="PTA-05",
        section=_S4,
        text=(
            "PTA-05 Trip changes must be re-approved if total cost increases "
            f"> {F.TRIP_CHANGE_REAPPROVAL_PCT}%."
        ),
    ),
    # ---- Section 5 — Air Travel -------------------------------------------
    CorpusUnit(
        id="AIR-01",
        section=_S5,
        text=(
            "AIR-01 Cabin class — standard: Economy for all flights with "
            f"scheduled flight time < {F.CABIN_ECONOMY_MAX_HOURS} hours."
        ),
    ),
    CorpusUnit(
        id="AIR-02",
        section=_S5,
        text=(
            "AIR-02 Premium economy: permitted for flights "
            f"{F.CABIN_PREMIUM_MIN_HOURS}–{F.CABIN_PREMIUM_MAX_HOURS} hours "
            "with manager approval."
        ),
    ),
    CorpusUnit(
        id="AIR-03",
        section=_S5,
        text=(
            "AIR-03 Business class: permitted for flights "
            f"> {F.CABIN_BUSINESS_MIN_HOURS} hours, OR for any flight where the "
            "traveler arrives < 8 hours before a critical business commitment, "
            "with VP approval. First class is never reimbursable."
        ),
    ),
    CorpusUnit(
        id="AIR-04",
        section=_S5,
        text=(
            "AIR-04 Role exception: SVPs and above may book business class on "
            f"flights > {F.SVP_BUSINESS_MIN_HOURS} hours."
        ),
    ),
    CorpusUnit(
        id="AIR-05",
        section=_S5,
        text=(
            "AIR-05 Preferred carriers: book Helios preferred airlines (Star "
            "Alliance partners + negotiated carriers) when within "
            f"{F.PREFERRED_FARE_PCT}% of the lowest logical fare."
        ),
    ),
    CorpusUnit(
        id="AIR-06",
        section=_S5,
        text=(
            "AIR-06 Lowest logical fare: travelers must accept fares within "
            f"{F.usd(F.LOWEST_FARE_TOLERANCE_DOMESTIC)} (domestic) / "
            f"{F.usd(F.LOWEST_FARE_TOLERANCE_INTL)} (international) of the "
            "cheapest option meeting routing/timing needs."
        ),
    ),
    CorpusUnit(
        id="AIR-07",
        section=_S5,
        text=(
            "AIR-07 Ancillary fees: one checked bag, standard seat selection, "
            "and Wi-Fi are reimbursable. Preferred/extra-legroom seats "
            "reimbursable only on flights > 6 hours. Early-boarding and lounge "
            "fees are not reimbursable (except for SVPs+ or delays > "
            f"{F.LOUNGE_DELAY_EXCEPTION_HOURS} hours)."
        ),
    ),
    CorpusUnit(
        id="AIR-08",
        section=_S5,
        text=(
            "AIR-08 Changes/cancellations: change fees are reimbursable for "
            "business-driven changes; cancellations must go through the TMC to "
            "preserve ticket value. Unused tickets must be reported, not "
            "discarded."
        ),
    ),
    # ---- Section 6 — Rail & Ground Transport ------------------------------
    CorpusUnit(
        id="GRND-01",
        section=_S6,
        text=(
            "GRND-01 Rail: Standard/Economy class for trips "
            f"< {F.RAIL_FIRST_MIN_HOURS} hours; First/Business rail permitted "
            f"for trips ≥ {F.RAIL_FIRST_MIN_HOURS} hours if cheaper than or "
            "comparable to flying. Rail preferred over air where rail "
            f"≤ {F.RAIL_PREFER_OVER_AIR_HOURS} hours (sustainability)."
        ),
    ),
    CorpusUnit(
        id="GRND-02",
        section=_S6,
        text=(
            "GRND-02 Rideshare/taxi: Standard tier (UberX/Lyft, standard taxi); "
            "premium tiers (Uber Black) not reimbursable except for safety or "
            "group needs."
        ),
    ),
    CorpusUnit(
        id="GRND-03",
        section=_S6,
        text=(
            "GRND-03 Airport transfers: rideshare/taxi/public transit "
            "reimbursable; airport parking reimbursable up to "
            f"{F.AIRPORT_PARKING_MAX_DAYS} days (economy lot)."
        ),
    ),
    CorpusUnit(
        id="GRND-04",
        section=_S6,
        text=("GRND-04 Public transit is encouraged and fully reimbursable."),
    ),
    CorpusUnit(
        id="GRND-05",
        section=_S6,
        text=(
            "GRND-05 Tolls and reasonable tips (≤ "
            f"{F.TIP_MAX_PCT}%) are reimbursable with the ride."
        ),
    ),
    CorpusUnit(
        id="GRND-06",
        section=_S6,
        text=(
            "GRND-06 Traffic/parking fines and personal commuting costs are " "never reimbursable."
        ),
    ),
    # ---- Section 7 — Rental Cars ------------------------------------------
    CorpusUnit(
        id="CAR-01",
        section=_S7,
        text=(
            "CAR-01 Class: Economy/Intermediate (compact/midsize). Full-size "
            "permitted for 3+ travelers or significant equipment; "
            "SUV/Premium requires manager approval."
        ),
    ),
    CorpusUnit(
        id="CAR-02",
        section=_S7,
        text=(
            "CAR-02 Preferred vendors: [Avis/Hertz/Enterprise corporate codes]; "
            "use to access negotiated rates and insurance."
        ),
    ),
    CorpusUnit(
        id="CAR-03",
        section=_S7,
        text=(
            "CAR-03 Insurance: decline personal-effects/PAI; accept CDW/LDW for "
            "international rentals and where corporate insurance does not apply "
            "(it does apply for US domestic via the corporate card)."
        ),
    ),
    CorpusUnit(
        id="CAR-04",
        section=_S7,
        text=(
            "CAR-04 Fuel: refuel before return; pre-paid fuel options are not "
            "reimbursable. EV charging reimbursable."
        ),
    ),
    CorpusUnit(
        id="CAR-05",
        section=_S7,
        text=("CAR-05 GPS/upgrades are not reimbursable (use phone navigation)."),
    ),
    # ---- Section 8 — Personal Vehicle & Mileage ---------------------------
    CorpusUnit(
        id="MIL-01",
        section=_S8,
        text=(
            "MIL-01 Mileage rate: reimbursed at the prevailing IRS standard "
            "mileage rate (US) / local statutory rate, updated annually."
        ),
    ),
    CorpusUnit(
        id="MIL-02",
        section=_S8,
        text=(
            "MIL-02 Basis: business miles only; normal commute miles are "
            "deducted from any claim."
        ),
    ),
    CorpusUnit(
        id="MIL-03",
        section=_S8,
        text=(
            "MIL-03 Cap: for trips "
            f"> {F.MILEAGE_AIR_CAP_MILES} miles each way, reimbursement is "
            "capped at the equivalent lowest logical airfare unless air travel "
            "was impractical."
        ),
    ),
    # ---- Section 9 — Hotels & Lodging -------------------------------------
    CorpusUnit(
        id="LOD-01",
        section=_S9,
        text=(
            "LOD-01 Nightly rate caps (room + tax): "
            f"Tier 1 {F.usd(F.LODGING_CAP['tier1'])}/night; "
            f"Tier 2 {F.usd(F.LODGING_CAP['tier2'])}/night; "
            f"Tier 3 {F.usd(F.LODGING_CAP['tier3'])}/night. "
            f"New York is Tier 1 → {F.usd(F.LODGING_CAP['tier1'])}/night."
        ),
    ),
    CorpusUnit(
        id="LOD-02",
        section=_S9,
        text=(
            "LOD-02 Exceptions: conference hotels and rates exceeding the cap "
            f"by ≤ {F.LODGING_CAP_AUTO_EXCEPTION_PCT}% are auto-approved when "
            f"booked through the tool; > {F.LODGING_CAP_AUTO_EXCEPTION_PCT}% "
            "requires manager approval with justification."
        ),
    ),
    CorpusUnit(
        id="LOD-03",
        section=_S9,
        text=(
            "LOD-03 Preferred chains: Marriott, Hilton, IHG, Accor (corporate "
            "rates); book preferred when within cap."
        ),
    ),
    CorpusUnit(
        id="LOD-04",
        section=_S9,
        text=(
            "LOD-04 Incidentals: in-room Wi-Fi, one daily breakfast (if not in "
            "per diem), and reasonable laundry on trips > 5 days are "
            "reimbursable. Minibar, movies, spa, and gym fees are not."
        ),
    ),
    CorpusUnit(
        id="LOD-05",
        section=_S9,
        text=(
            "LOD-05 Room type: standard room only; suites/upgrades not "
            "reimbursable except SVPs+."
        ),
    ),
    CorpusUnit(
        id="LOD-06",
        section=_S9,
        text=(
            "LOD-06 Alternative lodging: Airbnb/serviced apartments allowed if "
            "total cost ≤ the applicable hotel cap; verify safety and "
            "business-suitability."
        ),
    ),
    CorpusUnit(
        id="LOD-07",
        section=_S9,
        text=(
            "LOD-07 No-show / late cancellation charges are reimbursable only "
            "for business reasons; otherwise personal."
        ),
    ),
    # ---- Section 10 — Meals & Per Diem ------------------------------------
    CorpusUnit(
        id="MEAL-01",
        section=_S10,
        text=(
            "MEAL-01 Per diem (meals & incidentals): "
            f"Tier 1 {F.usd(F.PER_DIEM['tier1'])}/day; "
            f"Tier 2 {F.usd(F.PER_DIEM['tier2'])}/day; "
            f"Tier 3 {F.usd(F.PER_DIEM['tier3'])}/day. "
            "International per diems follow ECA rates by city."
        ),
    ),
    CorpusUnit(
        id="MEAL-02",
        section=_S10,
        text=(
            f"MEAL-02 Partial days: {F.PER_DIEM_PARTIAL_PCT}% of per diem on "
            "departure and return days."
        ),
    ),
    CorpusUnit(
        id="MEAL-03",
        section=_S10,
        text=(
            "MEAL-03 Receipt vs per diem: the company uses per diem by default; "
            "itemized meal reimbursement is allowed only where required by "
            "local tax law and is capped at the per diem amount."
        ),
    ),
    CorpusUnit(
        id="MEAL-04",
        section=_S10,
        text=(
            "MEAL-04 Alcohol: personal alcohol is not reimbursable under per "
            "diem or individual meals; alcohol is reimbursable only as part of "
            "approved client entertainment (Section 12) with attendee "
            "documentation and reasonable quantities."
        ),
    ),
    CorpusUnit(
        id="MEAL-05",
        section=_S10,
        text=(
            "MEAL-05 Team meals: the most senior employee present pays and "
            "expenses, listing all attendees; per-head dinner caps: "
            f"Tier 1 {F.usd(F.TEAM_DINNER_CAP['tier1'])}, "
            f"Tier 2 {F.usd(F.TEAM_DINNER_CAP['tier2'])}, "
            f"Tier 3 {F.usd(F.TEAM_DINNER_CAP['tier3'])}."
        ),
    ),
    CorpusUnit(
        id="MEAL-06",
        section=_S10,
        text=(
            "MEAL-06 Provided meals: per diem is reduced for any meal provided "
            "by a conference, airline, or host."
        ),
    ),
    # ---- Section 11 — International Travel ---------------------------------
    CorpusUnit(
        id="INTL-01",
        section=_S11,
        text=(
            "INTL-01 Visas/passports: business visa and entry-requirement "
            "costs are reimbursable; expedite fees reimbursable when "
            "time-critical. Passport renewals are personal unless required "
            "solely for company travel."
        ),
    ),
    CorpusUnit(
        id="INTL-02",
        section=_S11,
        text=(
            "INTL-02 Currency: reimbursed at the card's actual conversion rate "
            "or the trip-dated reference rate; corporate-card "
            "foreign-transaction fees are reimbursable."
        ),
    ),
    CorpusUnit(
        id="INTL-03",
        section=_S11,
        text=(
            "INTL-03 Mobile/roaming: an international roaming plan or local "
            "eSIM is reimbursable; personal-call overages are not."
        ),
    ),
    CorpusUnit(
        id="INTL-04",
        section=_S11,
        text=(
            "INTL-04 Travel insurance: company-provided business travel "
            "insurance covers all approved international trips; register the "
            "trip in the travel tool to be covered."
        ),
    ),
    CorpusUnit(
        id="INTL-05",
        section=_S11,
        text=(
            "INTL-05 Health & vaccinations: required vaccinations/prophylaxis "
            "for a destination are reimbursable."
        ),
    ),
    CorpusUnit(
        id="INTL-06",
        section=_S11,
        text=(
            "INTL-06 High-risk destinations: travel to locations under a "
            "Level 3/4 advisory requires VP + Security approval and a "
            "mandatory pre-trip briefing; some destinations are prohibited."
        ),
    ),
    CorpusUnit(
        id="INTL-07",
        section=_S11,
        text=(
            "INTL-07 Duty of care: travelers must keep itineraries current in "
            "the tool so the company can locate and assist them in "
            "emergencies."
        ),
    ),
    CorpusUnit(
        id="INTL-08",
        section=_S11,
        text=(
            "INTL-08 Per diem & lodging use ECA international rates by city; "
            "where unavailable, default to the nearest tier."
        ),
    ),
    # ---- Section 12 — Entertainment & Client Hospitality ------------------
    CorpusUnit(
        id="ENT-01",
        section=_S12,
        text=(
            "ENT-01 Client entertainment must have a clear business purpose and "
            "all attendees (names, companies) documented."
        ),
    ),
    CorpusUnit(
        id="ENT-02",
        section=_S12,
        text=(
            "ENT-02 Caps: client meals up to "
            f"{F.usd(F.CLIENT_MEAL_CAP['tier1'])}/head (Tier 1) / "
            f"{F.usd(F.CLIENT_MEAL_CAP['tier2'])} (Tier 2) / "
            f"{F.usd(F.CLIENT_MEAL_CAP['tier3'])} (Tier 3); larger events "
            "require pre-approval."
        ),
    ),
    CorpusUnit(
        id="ENT-03",
        section=_S12,
        text=(
            "ENT-03 Alcohol is permitted in moderation within client "
            "entertainment; intoxication and after-party costs are not "
            "reimbursable."
        ),
    ),
    CorpusUnit(
        id="ENT-04",
        section=_S12,
        text=(
            "ENT-04 Anti-bribery: entertainment of government officials "
            "requires Legal/Compliance pre-approval (FCPA / UK Bribery Act). "
            f"Gifts > {F.usd(F.GIFT_DISCLOSURE_THRESHOLD)} require disclosure."
        ),
    ),
    # ---- Section 13 — Conferences, Events & Training ----------------------
    CorpusUnit(
        id="CONF-01",
        section=_S13,
        text=(
            "CONF-01 Conference/training registration is reimbursable with "
            "manager approval; book early-bird rates."
        ),
    ),
    CorpusUnit(
        id="CONF-02",
        section=_S13,
        text=(
            "CONF-02 Conference-hotel rates above standard caps are allowed "
            "when booked through the event block (see LOD-02)."
        ),
    ),
    CorpusUnit(
        id="CONF-03",
        section=_S13,
        text=(
            "CONF-03 Optional add-ons (tours, galas) are reimbursable only if "
            "business-relevant and pre-approved."
        ),
    ),
    # ---- Section 14 — Expense Submission & Documentation ------------------
    CorpusUnit(
        id="EXP-01",
        section=_S14,
        text=(
            "EXP-01 Deadline: submit expense reports within "
            f"{F.SUBMISSION_DEADLINE_DAYS} days of trip completion; reports "
            f"> {F.STALE_REPORT_DAYS} days old require VP approval and may be "
            "treated as taxable."
        ),
    ),
    CorpusUnit(
        id="EXP-02",
        section=_S14,
        text=(
            "EXP-02 Receipt threshold: itemized receipts required for any "
            f"expense ≥ {F.usd(F.RECEIPT_THRESHOLD)}; hotel folios and airfare "
            "require receipts regardless of amount."
        ),
    ),
    CorpusUnit(
        id="EXP-03",
        section=_S14,
        text=(
            "EXP-03 Documentation: each expense needs date, amount, vendor, "
            "business purpose, and attendees (for meals/entertainment)."
        ),
    ),
    CorpusUnit(
        id="EXP-04",
        section=_S14,
        text=(
            "EXP-04 Lost receipts: a signed missing-receipt affidavit is "
            f"allowed up to {F.usd(F.AFFIDAVIT_MAX_AMOUNT)} and no more than "
            f"{F.AFFIDAVIT_MAX_PER_QUARTER} times per quarter."
        ),
    ),
    CorpusUnit(
        id="EXP-05",
        section=_S14,
        text=(
            "EXP-05 Per diem claims do not require meal receipts but do require "
            "travel dates and locations."
        ),
    ),
    CorpusUnit(
        id="EXP-06",
        section=_S14,
        text=(
            "EXP-06 Accuracy: the traveler certifies the report is accurate; "
            "managers are accountable for what they approve."
        ),
    ),
    # ---- Section 15 — Reimbursement Process & Timelines -------------------
    CorpusUnit(
        id="REIMB-01",
        section=_S15,
        text=(
            "REIMB-01 Manager approval → Finance audit → payment. Target "
            f"reimbursement within {F.REIMBURSEMENT_DAYS} business days of "
            "final approval via direct deposit."
        ),
    ),
    CorpusUnit(
        id="REIMB-02",
        section=_S15,
        text=(
            "REIMB-02 Disputed/flagged items are returned with a reason; "
            "corrected resubmission restarts the clock."
        ),
    ),
    CorpusUnit(
        id="REIMB-03",
        section=_S15,
        text=(
            "REIMB-03 Corporate-card expenses are paid directly (no personal "
            "reimbursement); personal-card expenses are reimbursed to the "
            "employee."
        ),
    ),
    # ---- Section 16 — Corporate Card Policy -------------------------------
    CorpusUnit(
        id="CARD-01",
        section=_S16,
        text=(
            "CARD-01 Eligible frequent travelers receive a corporate card; it "
            "is for business expenses only."
        ),
    ),
    CorpusUnit(
        id="CARD-02",
        section=_S16,
        text=(
            "CARD-02 Personal use is prohibited; accidental personal charges "
            "must be repaid immediately."
        ),
    ),
    CorpusUnit(
        id="CARD-03",
        section=_S16,
        text=(
            "CARD-03 Reconcile all card transactions in an expense report "
            "within the EXP-01 deadline."
        ),
    ),
    CorpusUnit(
        id="CARD-04",
        section=_S16,
        text=(
            "CARD-04 Lost/stolen cards must be reported to the issuer and "
            f"Finance within {F.CARD_LOSS_REPORT_HOURS} hours."
        ),
    ),
    CorpusUnit(
        id="CARD-05",
        section=_S16,
        text=("CARD-05 Cash advances on the corporate card are not permitted."),
    ),
    # ---- Section 17 — Non-Reimbursable Expenses ---------------------------
    CorpusUnit(
        id="NR-01",
        section=_S17,
        text=(
            "NR-01 Not reimbursable (non-exhaustive): personal alcohol; "
            "minibar, in-room movies, spa/gym; traffic/parking fines; "
            "flight/hotel upgrades beyond policy; first-class air; lounge "
            "memberships (except SVPs+); personal entertainment; personal "
            "grooming; childcare/pet care; loyalty-program fees; lost personal "
            "items; seat upgrades on short flights; expenses of accompanying "
            "family; commuting to the regular office; late fees from personal "
            "delays; expedited passport renewals (unless required solely for "
            f"company travel); excessive tips (> {F.TIP_MAX_PCT}%)."
        ),
    ),
    # ---- Section 18 — Exceptions, Violations & Consequences ---------------
    CorpusUnit(
        id="POL-01",
        section=_S18,
        text=(
            "POL-01 Exceptions: request via the travel/expense tool with "
            "justification; minor exceptions approved by manager, material "
            "ones by VP + Finance. Exceptions are logged and audited."
        ),
    ),
    CorpusUnit(
        id="POL-02",
        section=_S18,
        text=(
            "POL-02 Violations: out-of-policy spend may be partially or fully "
            "non-reimbursed; the employee may bear the difference."
        ),
    ),
    CorpusUnit(
        id="POL-03",
        section=_S18,
        text=(
            "POL-03 Escalating consequences: repeated violations → coaching → "
            "formal warning → disciplinary action. Fraud (falsified receipts, "
            "fictitious expenses) → immediate disciplinary action up to "
            "termination and possible legal action."
        ),
    ),
    CorpusUnit(
        id="POL-04",
        section=_S18,
        text=(
            "POL-04 Audit: Finance audits a sample of reports monthly; all "
            f"reports are subject to audit for {F.AUDIT_RETENTION_YEARS} years."
        ),
    ),
    # ---- Section 19 — Sustainability & Carbon -----------------------------
    CorpusUnit(
        id="SUS-01",
        section=_S19,
        text=(
            "SUS-01 Prefer rail over air where rail "
            f"≤ {F.RAIL_PREFER_OVER_AIR_HOURS} hours; prefer non-stop and "
            "economy (lower per-seat carbon)."
        ),
    ),
    CorpusUnit(
        id="SUS-02",
        section=_S19,
        text=(
            "SUS-02 The travel tool displays per-trip carbon estimates; teams "
            "have annual carbon budgets reported quarterly."
        ),
    ),
    CorpusUnit(
        id="SUS-03",
        section=_S19,
        text=(
            "SUS-03 Prefer hotels with recognized sustainability certification "
            "and EV/hybrid rental options where price-comparable."
        ),
    ),
    # ---- Section 20 — Data, Privacy & Contacts ----------------------------
    CorpusUnit(
        id="DATA-01",
        section=_S20,
        text=(
            "DATA-01 Travel/expense data is processed per the Helios Privacy "
            "Policy and applicable law (GDPR/CCPA); retained "
            f"{F.AUDIT_RETENTION_YEARS} years for tax/audit."
        ),
    ),
    CorpusUnit(
        id="DATA-02",
        section=_S20,
        text=(
            "DATA-02 Personal data of travelers is restricted to Finance, the "
            "TMC, and approvers on a need-to-know basis."
        ),
    ),
    CorpusUnit(
        id="CONT-01",
        section=_S20,
        text=(
            "CONT-01 Contacts: Travel Desk "
            f"{F.TRAVEL_DESK_EMAIL} / {F.TRAVEL_DESK_PHONE} (24/7); "
            f"Finance/AP {F.FINANCE_EMAIL}; "
            f"Compliance {F.COMPLIANCE_EMAIL}."
        ),
    ),
    CorpusUnit(
        id="CONT-02",
        section=_S20,
        text=(
            "CONT-02 Escalation: policy questions not answered by Atlas or this "
            "document → your manager, then the Travel Desk, then Global "
            "Finance."
        ),
    ),
)

DOCUMENT = CorpusDocument(
    doc_id=F.POLICY_DOC_ID,
    document_type="policy",
    title="Helios Global Travel & Expense Policy",
    version=1,
    effective_from=F.EFFECTIVE_FROM,
    header=(
        f"GLOBAL TRAVEL & EXPENSE (T&E) POLICY — {F.COMPANY}\n"
        f"Document ID: {F.POLICY_DOC_ID} · Version {F.POLICY_VERSION_LABEL} · "
        f"Effective {F.EFFECTIVE_FROM}"
    ),
    units=_UNITS,
)
