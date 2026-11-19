"""Helios Expense & Travel How-To Guide (document (c) of the Atlas RAG corpus).

Original fresh-authored synthetic content for Helios Dynamics, Inc.  This is the
process / interface companion to the authoritative policy (TEP-FY26): it tells a
traveler *how* to operate the booking and expense tools, while the policy tells
them *what* the rules are.  Every figure is interpolated from figures.py so the
how-to can never drift from the policy.

Exports a single module-level ``DOCUMENT: CorpusDocument`` with exactly 35
procedures (HOW-01 … HOW-35).
"""

from __future__ import annotations

from database.seeds.corpus import figures as F
from database.seeds.corpus._types import CorpusDocument, CorpusUnit

_UNITS: tuple[CorpusUnit, ...] = (
    CorpusUnit(
        id="HOW-01",
        section="Expense Reports",
        text=(
            "HOW-01 — Submit an expense report\n"
            "1. Open the Expense app and click 'New Report'; name it for the trip "
            "(e.g. 'NYC client visit, Mar 2026').\n"
            "2. Add each line item with date, amount, vendor, category, and a "
            "one-line business purpose (the tool blocks submission if any is "
            "blank — see EXP-03).\n"
            "3. Attach an itemized receipt to every line at or above "
            f"{F.usd(F.RECEIPT_THRESHOLD)}; airfare and hotel folios always need "
            "one (EXP-02).\n"
            "4. Click 'Check Compliance' to clear any amber/red flags before "
            "you send it.\n"
            f"5. Submit within {F.SUBMISSION_DEADLINE_DAYS} days of the trip end "
            "date (EXP-01); the report then routes to your manager for "
            "approval."
        ),
    ),
    CorpusUnit(
        id="HOW-02",
        section="Receipts",
        text=(
            "HOW-02 — Upload a receipt\n"
            "1. From a report line, tap 'Attach Receipt', or email the image to "
            "receipts@heliosdynamics.com from your work address to auto-match.\n"
            "2. Use the mobile camera capture so OCR can read the merchant, "
            "date, and total; review the parsed fields it fills in.\n"
            "3. Confirm the receipt is itemized (not just a card slip) for any "
            f"expense at or above {F.usd(F.RECEIPT_THRESHOLD)} (EXP-02).\n"
            "4. If OCR mis-reads a value, click the field and correct it; the "
            "original image stays attached for audit."
        ),
    ),
    CorpusUnit(
        id="HOW-03",
        section="Booking & Approvals",
        text=(
            "HOW-03 — Request pre-trip approval (PTA)\n"
            "1. In the Travel tool choose 'New Trip' and enter your dates, "
            "destination, and estimated cost.\n"
            "2. Click 'Request Pre-Trip Approval'; the request routes to your "
            "manager (PTA-01).\n"
            f"3. Submit it at least {F.ADVANCE_BOOKING_DOMESTIC_DAYS} days ahead "
            f"for domestic and {F.ADVANCE_BOOKING_INTL_DAYS} days ahead for "
            "international trips so you capture advance-purchase fares "
            "(PTA-02).\n"
            "4. For international or high-risk travel, the request also routes to "
            "your VP and the Travel Desk security review (PTA-04).\n"
            "5. Wait for the 'Approved' status before you book — the booking "
            "screen unlocks once approval is recorded."
        ),
    ),
    CorpusUnit(
        id="HOW-04",
        section="Corporate Card",
        text=(
            "HOW-04 — Add and activate a corporate card\n"
            "1. Go to Profile > Payment Methods > 'Add Corporate Card'.\n"
            "2. Enter the embossed name and last four digits; the card feed is "
            "linked by Finance, so you do not type the full number.\n"
            "3. Activate the physical card by calling the issuer number on the "
            "sticker, then set your PIN.\n"
            "4. Confirm the card is flagged 'Business use only' (CARD-01); "
            "personal charges are prohibited (CARD-02)."
        ),
    ),
    CorpusUnit(
        id="HOW-05",
        section="Corporate Card",
        text=(
            "HOW-05 — Reconcile corporate-card transactions\n"
            "1. Open 'Card Transactions'; imported charges appear as unassigned "
            "lines.\n"
            "2. Drag each charge onto the matching expense report, or click "
            "'Create Report' to start one.\n"
            "3. Add the business purpose and attach receipts as for any other "
            "line.\n"
            f"4. Reconcile every charge within {F.CARD_RECONCILE_DEADLINE_DAYS} "
            "days, inside the standard submission window (CARD-03).\n"
            "5. If a personal charge slipped through, mark it 'Personal' and "
            "repay it immediately via the 'Owe Company' link (CARD-02)."
        ),
    ),
    CorpusUnit(
        id="HOW-06",
        section="Mileage",
        text=(
            "HOW-06 — Log mileage\n"
            "1. On a report click 'Add Mileage' and pick the trip date.\n"
            "2. Enter start and end addresses; the tool maps the route and "
            "applies the prevailing IRS standard rate (MIL-01).\n"
            "3. Deduct your normal commute miles using the 'Commute Offset' "
            "field (MIL-02).\n"
            f"4. For one-way trips over {F.MILEAGE_AIR_CAP_MILES} miles the tool "
            "caps the claim at the equivalent lowest logical airfare unless you "
            "flag air travel as impractical (MIL-03)."
        ),
    ),
    CorpusUnit(
        id="HOW-07",
        section="Travel Tools",
        text=(
            "HOW-07 — Book a flight through the tool\n"
            "1. After approval, open the trip and click 'Search Flights'; enter "
            "your route and times.\n"
            "2. The tool sorts by lowest logical fare and badges preferred "
            "carriers (AIR-05).\n"
            "3. Accept a fare within the lowest-logical-fare tolerance "
            f"({F.usd(F.LOWEST_FARE_TOLERANCE_DOMESTIC)} domestic / "
            f"{F.usd(F.LOWEST_FARE_TOLERANCE_INTL)} international) of the "
            "cheapest compliant option (AIR-06).\n"
            "4. Cabin defaults to economy under 6 hours; the tool offers "
            "premium economy or business only where your flight time and role "
            "qualify (AIR-01..AIR-04).\n"
            "5. Confirm and the e-ticket lands in your itinerary."
        ),
    ),
    CorpusUnit(
        id="HOW-08",
        section="Travel Tools",
        text=(
            "HOW-08 — Book a hotel through the tool\n"
            "1. From the trip click 'Search Hotels' and set your dates and "
            "city.\n"
            "2. Results are filtered to the nightly cap for the destination "
            "tier — Tier 1 "
            f"{F.usd(F.LODGING_CAP['tier1'])}, Tier 2 "
            f"{F.usd(F.LODGING_CAP['tier2'])}, Tier 3 "
            f"{F.usd(F.LODGING_CAP['tier3'])} room + tax (LOD-01).\n"
            "3. Preferred chains are badged; book one within cap when "
            "available (LOD-03).\n"
            "4. A rate up to 15% over cap auto-approves through the tool; above "
            "that, add a justification for manager approval (LOD-02).\n"
            "5. Choose a standard room and confirm the reservation."
        ),
    ),
    CorpusUnit(
        id="HOW-09",
        section="Receipts",
        text=(
            "HOW-09 — File a missing-receipt affidavit\n"
            "1. On the affected line click 'No Receipt' > 'File Affidavit'.\n"
            "2. State what the expense was, why the receipt is unavailable, and "
            "certify the amount.\n"
            "3. Affidavits are accepted only for amounts at or below "
            f"{F.usd(F.AFFIDAVIT_MAX_AMOUNT)} (EXP-04).\n"
            f"4. You may file at most {F.AFFIDAVIT_MAX_PER_QUARTER} affidavits "
            "per quarter; the tool shows your remaining count.\n"
            "5. Sign electronically; the affidavit attaches to the line in "
            "place of the receipt."
        ),
    ),
    CorpusUnit(
        id="HOW-10",
        section="Expense Reports",
        text=(
            "HOW-10 — Request a policy exception\n"
            "1. On a flagged line or trip click 'Request Exception'.\n"
            "2. Pick the rule being excepted and write a clear business "
            "justification (POL-01).\n"
            "3. Minor exceptions route to your manager; material ones route to "
            "your VP and Finance.\n"
            "4. The request, decision, and reason are logged for audit; track "
            "status under 'My Exceptions'."
        ),
    ),
    CorpusUnit(
        id="HOW-11",
        section="Expense Reports",
        text=(
            "HOW-11 — Split a personal charge\n"
            "1. Open the mixed line and click 'Itemize / Split'.\n"
            "2. Add a 'Personal (non-reimbursable)' portion and a 'Business' "
            "portion so the totals match the receipt.\n"
            "3. The personal portion is excluded from reimbursement (PRIN-02); "
            "if it was on the corporate card, mark it 'Owe Company'.\n"
            "4. Save — the business portion stays on the report and routes "
            "normally."
        ),
    ),
    CorpusUnit(
        id="HOW-12",
        section="Expense Reports",
        text=(
            "HOW-12 — Add attendees to a client meal\n"
            "1. On the meal line set the category to 'Client Entertainment'.\n"
            "2. Click 'Attendees' and add each guest's name and company, plus "
            "internal attendees (ENT-01).\n"
            "3. The tool checks per-head spend against the client-meal cap — "
            f"Tier 1 {F.usd(F.CLIENT_MEAL_CAP['tier1'])}, Tier 2 "
            f"{F.usd(F.CLIENT_MEAL_CAP['tier2'])}, Tier 3 "
            f"{F.usd(F.CLIENT_MEAL_CAP['tier3'])} per head (ENT-02).\n"
            "4. Note the business purpose; alcohol is allowed only here, in "
            "moderation (ENT-03)."
        ),
    ),
    CorpusUnit(
        id="HOW-13",
        section="Travel Tools",
        text=(
            "HOW-13 — Check a trip's carbon estimate\n"
            "1. Open the trip and click the 'Sustainability' tab.\n"
            "2. The tool shows the per-trip carbon estimate for your selected "
            "itinerary (SUS-02).\n"
            "3. Where rail is 4 hours or less it suggests rail over air to cut "
            "emissions (SUS-01).\n"
            "4. Compare options and pick the lower-carbon, price-comparable "
            "choice; your team's quarterly carbon budget updates automatically."
        ),
    ),
    CorpusUnit(
        id="HOW-14",
        section="Corporate Card",
        text=(
            "HOW-14 — Report a lost or stolen card\n"
            "1. In Profile > Payment Methods click 'Report Lost / Stolen' to "
            "freeze the card instantly.\n"
            "2. Call the issuer's 24/7 number shown on screen to cancel and "
            "request a replacement.\n"
            f"3. Notify Finance within {F.CARD_LOSS_REPORT_HOURS}h via the same "
            "screen so the feed is flagged (CARD-04).\n"
            "4. Review recent transactions and dispute any you do not "
            "recognize."
        ),
    ),
    CorpusUnit(
        id="HOW-15",
        section="Expense Reports",
        text=(
            "HOW-15 — Resubmit a returned report\n"
            "1. Open the report marked 'Returned'; read the reviewer's note on "
            "each flagged line (REIMB-02).\n"
            "2. Fix the items — correct an amount, add a receipt, or attach a "
            "justification.\n"
            "3. Re-run 'Check Compliance' to clear remaining flags.\n"
            "4. Click 'Resubmit'; approval and the reimbursement clock restart "
            "from this submission (REIMB-02)."
        ),
    ),
    CorpusUnit(
        id="HOW-16",
        section="Reimbursement",
        text=(
            "HOW-16 — Track reimbursement status\n"
            "1. Open 'My Reports' and watch the status badge move through "
            "Submitted > Approved > In Audit > Paid.\n"
            "2. After final approval, payment targets "
            f"{F.REIMBURSEMENT_DAYS} business days via direct deposit "
            "(REIMB-01).\n"
            "3. Corporate-card lines are paid directly to the issuer; "
            "personal-card lines reimburse to you (REIMB-03).\n"
            "4. Confirm your bank details under Profile > Banking if a payment "
            "is held."
        ),
    ),
    CorpusUnit(
        id="HOW-17",
        section="Booking & Approvals",
        text=(
            "HOW-17 — Change or cancel a booking\n"
            "1. Open the itinerary and click 'Change' or 'Cancel'; route "
            "complex changes through the TMC agent (AIR-08).\n"
            "2. If the change raises total trip cost by more than 10%, the tool "
            "re-routes it for approval (PTA-05).\n"
            "3. Cancel through the tool so unused ticket value is preserved, "
            "never discarded (AIR-08).\n"
            "4. Reconcile any change fee on your next report with the business "
            "reason noted."
        ),
    ),
    CorpusUnit(
        id="HOW-18",
        section="Travel Tools",
        text=(
            "HOW-18 — Book a rental car\n"
            "1. From the trip click 'Search Cars' and pick a preferred vendor "
            "to apply the corporate code (CAR-02).\n"
            "2. Choose economy or intermediate by default; full-size needs 3+ "
            "travelers or equipment, and SUV/premium needs manager approval "
            "(CAR-01).\n"
            "3. Decline personal-effects and PAI insurance; accept CDW/LDW for "
            "international rentals (CAR-03).\n"
            "4. Skip GPS and pre-paid fuel — they are not reimbursable "
            "(CAR-04, CAR-05)."
        ),
    ),
    CorpusUnit(
        id="HOW-19",
        section="Travel Tools",
        text=(
            "HOW-19 — Book rail travel\n"
            "1. Click 'Search Rail' and enter your route and times.\n"
            "2. For trips of 3 hours or more the tool offers first/business "
            "rail when it is comparable to or cheaper than flying (GRND-01).\n"
            "3. Where rail is 4 hours or less it is preferred over air for "
            "sustainability (GRND-01, SUS-01).\n"
            "4. Select your seat and confirm; the ticket joins your itinerary."
        ),
    ),
    CorpusUnit(
        id="HOW-20",
        section="Expense Reports",
        text=(
            "HOW-20 — Claim per diem instead of meal receipts\n"
            "1. On the report click 'Add Per Diem' and select your travel "
            "dates and city.\n"
            "2. The tool applies the daily rate for the city tier — Tier 1 "
            f"{F.usd(F.PER_DIEM['tier1'])}, Tier 2 "
            f"{F.usd(F.PER_DIEM['tier2'])}, Tier 3 "
            f"{F.usd(F.PER_DIEM['tier3'])} per day (MEAL-01).\n"
            "3. Departure and return days auto-apply 75% of the rate "
            "(MEAL-02).\n"
            "4. Tick any meal provided by a conference or host so the tool "
            "reduces that day (MEAL-06); no meal receipts are needed, only "
            "dates and locations (EXP-05)."
        ),
    ),
    CorpusUnit(
        id="HOW-21",
        section="Booking & Approvals",
        text=(
            "HOW-21 — Register itinerary for duty of care\n"
            "1. Confirm your trip is saved in the Travel tool so it appears on "
            "the duty-of-care map (INTL-07).\n"
            "2. Keep flight, hotel, and contact details current after any "
            "change.\n"
            "3. For international trips this also activates company travel "
            "insurance coverage (INTL-04).\n"
            "4. Enable trip notifications so the Travel Desk can reach you in "
            "an emergency."
        ),
    ),
    CorpusUnit(
        id="HOW-22",
        section="Booking & Approvals",
        text=(
            "HOW-22 — Request VP approval for international travel\n"
            "1. Mark the trip 'International' so the tool adds the VP approval "
            "and security-review steps (PTA-04).\n"
            "2. Attach the business case and any visa requirements (INTL-01).\n"
            "3. For Level 3/4 advisory destinations, complete the mandatory "
            "pre-trip security briefing the tool schedules (INTL-06).\n"
            "4. Book only after both approvals show 'Approved'."
        ),
    ),
    CorpusUnit(
        id="HOW-23",
        section="Expense Reports",
        text=(
            "HOW-23 — Expense a team meal as the senior host\n"
            "1. The most senior employee present pays and creates the line "
            "(MEAL-05).\n"
            "2. Set the category to 'Team Meal' and list every attendee.\n"
            "3. The tool checks per-head dinner spend against the cap — Tier 1 "
            f"{F.usd(F.TEAM_DINNER_CAP['tier1'])}, Tier 2 "
            f"{F.usd(F.TEAM_DINNER_CAP['tier2'])}, Tier 3 "
            f"{F.usd(F.TEAM_DINNER_CAP['tier3'])} per head (MEAL-05).\n"
            "4. Personal alcohol is not reimbursable on a team meal "
            "(MEAL-04)."
        ),
    ),
    CorpusUnit(
        id="HOW-24",
        section="Expense Reports",
        text=(
            "HOW-24 — Disclose a gift to a client\n"
            "1. Create a line under 'Gifts & Hospitality' with the recipient "
            "and business reason.\n"
            f"2. Any gift over {F.usd(F.GIFT_DISCLOSURE_THRESHOLD)} triggers the "
            "disclosure form (ENT-04).\n"
            "3. Gifts or entertainment involving government officials require "
            "Legal/Compliance pre-approval (FCPA / UK Bribery Act).\n"
            "4. Submit; the disclosure is logged with the expense for audit."
        ),
    ),
    CorpusUnit(
        id="HOW-25",
        section="Travel Tools",
        text=(
            "HOW-25 — Handle a travel emergency\n"
            "1. Call the Travel Desk 24/7 line shown under the trip's 'Help' "
            "menu (DEF-03).\n"
            "2. Use 'Rebook' for disruptions so the TMC preserves ticket value "
            "(AIR-08).\n"
            "3. Keep your itinerary current so the company can locate and "
            "assist you (INTL-07).\n"
            "4. Save any extra receipts to reconcile after you return."
        ),
    ),
    CorpusUnit(
        id="HOW-26",
        section="Expense Reports",
        text=(
            "HOW-26 — Expense currency conversions\n"
            "1. The tool converts foreign charges at the card's actual rate or "
            "the trip-dated reference rate (INTL-02).\n"
            "2. Leave the original-currency amount on the line; do not "
            "pre-convert.\n"
            "3. Corporate-card foreign-transaction fees are reimbursable — keep "
            "them on their own line (INTL-02).\n"
            "4. Attach the foreign receipt; OCR retains the original "
            "currency."
        ),
    ),
    CorpusUnit(
        id="HOW-27",
        section="Expense Reports",
        text=(
            "HOW-27 — Submit a stale (late) report\n"
            f"1. Reports older than {F.STALE_REPORT_DAYS} days from trip end "
            "are flagged 'Stale' and need VP approval (EXP-01).\n"
            "2. Add a justification explaining the delay when prompted.\n"
            "3. Be aware a stale reimbursement may be treated as taxable "
            "income (EXP-01).\n"
            f"4. Going forward, submit within {F.SUBMISSION_DEADLINE_DAYS} days "
            "of trip end to avoid the stale path."
        ),
    ),
    CorpusUnit(
        id="HOW-28",
        section="Booking & Approvals",
        text=(
            "HOW-28 — Book a conference and event hotel\n"
            "1. Register for the conference and add the cost as a line with "
            "manager approval (CONF-01).\n"
            "2. Book the event-block hotel through the tool; rates above the "
            "standard cap are allowed via the block (CONF-02, LOD-02).\n"
            "3. Add only business-relevant, pre-approved optional sessions or "
            "events (CONF-03).\n"
            "4. Keep the registration confirmation as the receipt."
        ),
    ),
    CorpusUnit(
        id="HOW-29",
        section="Receipts",
        text=(
            "HOW-29 — Itemize a hotel folio\n"
            "1. Attach the full folio (room + tax + incidentals), which is "
            "always required regardless of amount (EXP-02).\n"
            "2. Click 'Itemize' and split nightly room, taxes, and any "
            "incidentals into separate lines.\n"
            "3. Move minibar, movies, spa, and gym charges to 'Personal' — "
            "they are non-reimbursable (LOD-04).\n"
            "4. Keep in-room Wi-Fi and eligible breakfast on the business "
            "side (LOD-04)."
        ),
    ),
    CorpusUnit(
        id="HOW-30",
        section="Booking & Approvals",
        text=(
            "HOW-30 — Add a personal (bleisure) segment\n"
            "1. In the trip click 'Add Personal Days' and set the personal "
            "date range.\n"
            "2. The tool keeps the company portion at the would-be business "
            "cost and isolates any fare or room difference as personal "
            "(PRIN-03).\n"
            "3. Pay the personal difference at booking; only the business "
            "portion routes for reimbursement (PRIN-02).\n"
            "4. Confirm there is no extra cost to the company before you "
            "submit."
        ),
    ),
    CorpusUnit(
        id="HOW-31",
        section="Booking & Approvals",
        text=(
            "HOW-31 — Use the Online Booking Tool first\n"
            "1. Always start in the Online Booking Tool; out-of-channel "
            "bookings ('leakage') reimburse only with an approved exception "
            "(PRIN-01).\n"
            "2. For complex or multi-leg itineraries click 'Send to TMC Agent' "
            "(PTA-03).\n"
            "3. Compliant defaults are pre-selected; accept them unless you "
            "have a justified exception.\n"
            "4. If you must book out of channel, file the exception first "
            "(HOW-10)."
        ),
    ),
    CorpusUnit(
        id="HOW-32",
        section="Expense Reports",
        text=(
            "HOW-32 — Expense airport parking and ground transfers\n"
            "1. Add airport parking; the economy lot is reimbursable up to "
            f"{F.AIRPORT_PARKING_MAX_DAYS} days (GRND-03).\n"
            "2. Use standard-tier rideshare or taxi for transfers; premium "
            "tiers are not reimbursable except for safety or group needs "
            "(GRND-02).\n"
            "3. Tolls and reasonable tips up to 20% go on the ride line "
            "(GRND-05).\n"
            "4. Traffic and parking fines are never reimbursable (GRND-06)."
        ),
    ),
    CorpusUnit(
        id="HOW-33",
        section="Receipts",
        text=(
            "HOW-33 — Fix a compliance flag before submitting\n"
            "1. Click any amber or red flag to read the cited rule and what is "
            "missing.\n"
            "2. Resolve it inline — add the receipt, attendees, or business "
            "purpose, or split a personal portion.\n"
            "3. If the spend is justified but out of policy, attach an "
            "exception instead (HOW-10).\n"
            "4. Re-run 'Check Compliance' until the report is clear, then "
            "submit (EXP-06)."
        ),
    ),
    CorpusUnit(
        id="HOW-34",
        section="Reimbursement",
        text=(
            "HOW-34 — Repay an accidental personal charge\n"
            "1. On the corporate-card line click 'Mark Personal' (CARD-02).\n"
            "2. Use the 'Owe Company' link to repay immediately by bank "
            "transfer or payroll deduction.\n"
            "3. The line is excluded from reimbursement and the repayment is "
            "logged against it.\n"
            "4. Keep the card flagged 'Business use only' to avoid repeat "
            "charges (CARD-01)."
        ),
    ),
    CorpusUnit(
        id="HOW-35",
        section="Expense Reports",
        text=(
            "HOW-35 — Find your report at audit time\n"
            "1. Use 'My Reports' search by date, trip, or amount to locate a "
            "past report.\n"
            "2. All reports are retained and subject to audit for "
            f"{F.AUDIT_RETENTION_YEARS} years (POL-04, DATA-01).\n"
            "3. Open a report to view its full receipt and approval trail.\n"
            "4. Export to PDF from the report menu if Finance requests a copy "
            "during an audit."
        ),
    ),
)

DOCUMENT = CorpusDocument(
    doc_id="HOW-FY26",
    document_type="howto",
    title="Helios Expense & Travel How-To Guide",
    version=1,
    effective_from=F.EFFECTIVE_FROM,
    header=(
        "HELIOS EXPENSE & TRAVEL — HOW-TO GUIDE\n" f"{F.COMPANY} · Companion to {F.POLICY_DOC_ID}"
    ),
    units=_UNITS,
)
