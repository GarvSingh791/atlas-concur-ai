"""Pydantic request/response schemas for the expense module."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from ai_agents.tools.spend_policy import MAX_ATTENDEES, MAX_DISTANCE
from backend.app.core.money import AmountConversion
from backend.app.modules.receipt.schemas import PolicyVerdictSummary
from config.constants.fx_rates import SUPPORTED_CURRENCIES, UnknownCurrency, normalize_currency
from database.models.expense import ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReportStatus

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


def supported_currency(value: str | None) -> str | None:
    """Product gap 8: an upper-cased currency from the FX reference table, else 422.

    An unsupported code could never be converted for a report total or compared
    with a policy cap, so it is refused at the boundary (never stored as-is).
    """
    if value is None:
        return None
    try:
        return normalize_currency(value)
    except UnknownCurrency:
        raise ValueError(
            f"Currency {value!r} is not supported. Supported: {', '.join(SUPPORTED_CURRENCIES)}."
        ) from None


class ExpenseEntryCreate(BaseModel):
    category: ExpenseCategory
    amount: Decimal
    currency: str
    expense_date: date
    vendor: str | None = None
    description: str | None = None
    is_itemized: bool = False

    check_currency = field_validator("currency")(supported_currency)

    @field_validator("category")
    @classmethod
    def _no_client_mileage(cls, value: ExpenseCategory) -> ExpenseCategory:
        # Product gap 2: a mileage amount is computed server-side (MIL-01) — only
        # POST /expense-reports/{id}/mileage creates a mileage entry.
        if value == ExpenseCategory.mileage:
            raise ValueError("Add mileage with POST /expense-reports/{id}/mileage.")
        return value


class ExpenseReportCreate(BaseModel):
    title: str
    currency: str
    trip_id: uuid.UUID | None = None
    entries: list[ExpenseEntryCreate] = []

    check_currency = field_validator("currency")(supported_currency)


class ReportTransition(BaseModel):
    status: ExpenseReportStatus


JUSTIFICATION_MAX_LENGTH = 2000
PLACE_MAX_LENGTH = 200
DISCLOSURE_TEXT_MAX_LENGTH = 1000


# ---------------------------------------------------------------------------
# Product gap 2 (policy2): attendees / gift disclosure / mileage inputs
# ---------------------------------------------------------------------------


class DisclosureInput(BaseModel):
    """ENT-04 gift / hospitality disclosure (blank text -> null)."""

    model_config = ConfigDict(extra="forbid")

    recipient: str | None = Field(default=None, max_length=PLACE_MAX_LENGTH)
    recipient_organization: str | None = Field(default=None, max_length=PLACE_MAX_LENGTH)
    business_purpose: str | None = Field(default=None, max_length=DISCLOSURE_TEXT_MAX_LENGTH)
    government_official: bool | None = None

    @field_validator("recipient", "recipient_organization", "business_purpose")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


_DISTANCE = Field(gt=0, le=MAX_DISTANCE, max_digits=6, decimal_places=1)


class MileageInput(BaseModel):
    """One personal-vehicle trip (MIL-01..03).  There is NO amount / rate field: the
    server computes amount = business miles x the MIL-01 rate."""

    model_config = ConfigDict(extra="forbid")

    distance: Decimal = _DISTANCE  # one way
    unit: Literal["mi", "km"] = "mi"
    origin: str = Field(min_length=1, max_length=PLACE_MAX_LENGTH)
    destination: str = Field(min_length=1, max_length=PLACE_MAX_LENGTH)
    round_trip: bool = False
    # MIL-02: required (0 when the trip replaced no commute), same unit as distance.
    commute_offset: Decimal = Field(ge=0, le=MAX_DISTANCE, max_digits=6, decimal_places=1)
    air_impractical: bool = False

    @field_validator("origin", "destination")
    @classmethod
    def _strip_place(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("cannot be blank")
        return stripped


class MileageRateResponse(BaseModel):
    """GET /mileage-rate — the MIL-01 rate the server charges this organisation.

    Read-only, for the Add-mileage live estimate; the amount is still computed
    server-side on POST /expense-reports/{id}/mileage.
    """

    rate_per_mile: Decimal
    currency: str
    rate_clause: str


class MileageEntryCreate(MileageInput):
    """POST /expense-reports/{id}/mileage.  ``amount`` is optional and only CHECKED:
    a value that differs from the computed amount is refused (422)."""

    expense_date: date
    description: str | None = Field(default=None, max_length=500)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


REPORT_TITLE_MAX_LENGTH = 200

# Entry columns that are NOT NULL: omitting them leaves the value unchanged, but
# an explicit null is a client error (422), never a database error.
_NON_NULLABLE_ENTRY_FIELDS = ("category", "amount", "currency", "expense_date", "is_itemized")


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class ExpenseUpdate(BaseModel):
    """Partial update for an expense entry — all fields optional.

    ``justification`` (C7R UAT) is the claimant's written reason for a policy
    exception; a blank value clears it.  ``vendor`` / ``description`` blank → null.
    """

    category: ExpenseCategory | None = None
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    expense_date: date | None = None
    vendor: str | None = None
    description: str | None = None
    is_itemized: bool | None = None
    justification: str | None = Field(default=None, max_length=JUSTIFICATION_MAX_LENGTH)
    # EXP-04 (product gap 2 stage 2): true signs the missing-receipt affidavit (the
    # server records when), false withdraws it.  The justification is its explanation.
    receipt_affidavit: bool | None = None
    # Product gap 2: null clears.  Each is accepted only for its categories
    # (attendees: meals / entertainment; disclosure: gift / entertainment;
    # mileage: mileage entries — whose amount is then recomputed server-side).
    # Integration hardening: a strict int — JSON true / "3" / 3.0 are refused (422),
    # never coerced into a head count that multiplies a per-head cap.
    attendee_count: StrictInt | None = Field(default=None, ge=1, le=MAX_ATTENDEES)
    disclosure: DisclosureInput | None = None
    mileage: MileageInput | None = None

    @field_validator("vendor", "description", "justification")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    check_currency = field_validator("currency")(supported_currency)

    @model_validator(mode="after")
    def _no_null_for_required_columns(self) -> ExpenseUpdate:
        for field in _NON_NULLABLE_ENTRY_FIELDS:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"'{field}' cannot be null.")
        return self


class ReportDetailsUpdate(BaseModel):
    """PATCH /expense-reports/{id}/details — rename a draft report."""

    title: str = Field(min_length=1, max_length=REPORT_TITLE_MAX_LENGTH)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Title cannot be blank.")
        return stripped


class MoveEntriesRequest(BaseModel):
    """POST /expense-reports/{id}/entries/move — move entries to another draft report."""

    entry_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    target_report_id: uuid.UUID


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class EntryReceiptRemoval(BaseModel):
    """Whether the viewer may remove one receipt from an entry now (UAT2 retest B).

    Server-computed with the same rule as DELETE /receipts (owner, draft report,
    not kept for audit, and — on a report with approval history — not seen by a
    reviewer), so the editor offers Remove exactly where the server allows it.
    ``kept_for_audit``: refused because a reviewer has seen it.
    """

    receipt_id: uuid.UUID
    removable: bool = False
    kept_for_audit: bool = False
    removal_blocked_reason: str | None = None


class EntryReceipt(BaseModel):
    """One receipt backing an entry, as the reviewer needs it (UAT2 defect F).

    ``link``: ``direct`` (the receipt's own entry) or ``shared`` (an itemized
    sibling's folio split onto this line).  ``vendor`` / ``receipt_date`` /
    ``amount`` are the receipt's MACHINE reading (the frozen OCR snapshot, never a
    typed or AI-guessed value) — None where there is none.  ``covers_line``: the
    machine-read total covers this line's claim (RECEIPT-REQ's own rule, tip
    slack included); None when there is no machine-read total.
    ``attached_after_return``: no reviewer saw it — it arrived after the report's
    last return (``unreviewed_receipt_ids``).  The image itself is fetched via
    GET /receipts/{id}, which re-applies RBAC + record scoping.
    """

    receipt_id: uuid.UUID
    link: Literal["direct", "shared"]
    vendor: str | None = None
    receipt_date: date | None = None
    amount: Decimal | None = None
    currency: str | None = None
    covers_line: bool | None = None
    attached_after_return: bool = False


class ExpenseEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    report_id: uuid.UUID
    trip_segment_id: uuid.UUID | None
    category: ExpenseCategory
    amount: Decimal
    currency: str
    expense_date: date
    vendor: str | None
    description: str | None
    status: ExpenseStatus
    match_source: ExpenseMatchSource | None
    is_itemized: bool
    # C7R UAT: the claimant's written policy-exception justification (or null).
    justification: str | None = None
    # EXP-04: when the owner signed the missing-receipt affidavit (null = not signed).
    receipt_affidavit_at: datetime | None = None
    # Product gap 2: attendees (MEAL-05 / ENT-02), ENT-04 disclosure and the
    # mileage trip incl. the server-computed billable miles and rate (MIL-01..03).
    attendee_count: int | None = None
    disclosure: dict | None = None
    mileage: dict | None = None
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # First (earliest) receipt linked to this entry, if any. Not an ORM column —
    # populated by service.entries_with_receipt_ids so the owner / approver views
    # can fetch the scoped image link via GET /receipts/{id}.
    receipt_id: uuid.UUID | None = None
    # UAT2: every receipt linked to this entry (oldest first).  An entry may
    # carry a corrected receipt next to a wrong one; any covering one counts.
    receipt_ids: list[uuid.UUID] = []
    # UAT2 defect F: the same receipts (same order) with what the reviewer needs
    # to judge each — the machine reading, direct/shared, and whether it covers
    # the line or arrived after the last return.
    receipts: list[EntryReceipt] = []
    # UAT2: whether the owner may DELETE this entry right now (draft report, and
    # — on a report a reviewer returned — the return froze the entry's content),
    # with the reason when not.  Computed server-side so the UI never offers a
    # delete the server will refuse.
    deletable: bool = False
    delete_blocked_reason: str | None = None
    # UAT2 retest B: per receipt in ``receipt_ids`` (same order), whether the
    # viewer may remove it now.  Empty when not computed (approver views).
    receipt_removal: list[EntryReceiptRemoval] = []
    # Product gap 8: the amount in its report's currency when the entry is in
    # another one (simulated reference rate, shown with the rate); else None.
    amount_conversion: AmountConversion | None = None


class ExpenseReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    owner_id: uuid.UUID
    trip_id: uuid.UUID | None
    title: str
    status: ExpenseReportStatus
    total_amount: Decimal
    currency: str
    submitted_at: datetime | None
    pre_submit_flags: dict | None
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    version: int
    # C7R / P12 — the report's approval as seen by its owner (who has no
    # approval.read): current approval status (None = never sent for approval)
    # and, while the report is returned/rejected, the approver's reason.
    approval_status: str | None = None
    approval_reason: str | None = None
    # UAT2 — the owner's display name, so a non-owner reader (a manager via the
    # team read scope) sees whose report it is.  Read-only, server-derived.
    owner_name: str | None = None
    # Product gap 1 — simulated reimbursement (set by finance; no real money moves).
    reimbursed_at: datetime | None = None
    reimbursed_by_id: uuid.UUID | None = None
    reimbursement_reference: str | None = None
    reimbursement_amount: Decimal | None = None
    # Product gap 4 — the owner withdrew this RETURNED report (status stays
    # ``draft``; a non-null withdrawn_at means closed and read-only).
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None


class ReportListItem(BaseModel):
    """Lean list row for GET /expense-reports."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    owner_id: uuid.UUID
    trip_id: uuid.UUID | None
    title: str
    status: ExpenseReportStatus
    total_amount: Decimal
    currency: str
    submitted_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    # C7R / P12 — current approval status (None = never sent for approval).
    approval_status: str | None = None
    # Product gaps 1 / 4 — see ExpenseReportResponse.
    reimbursed_at: datetime | None = None
    reimbursement_reference: str | None = None
    withdrawn_at: datetime | None = None


class MoveEntriesResponse(BaseModel):
    """Both reports after a move — totals, pre-submit flags and versions are fresh."""

    source: ExpenseReportResponse
    target: ExpenseReportResponse
    moved_entry_ids: list[uuid.UUID]


class EntryDeleteResponse(BaseModel):
    """DELETE /expenses/{id} — the parent report after the delete (UAT2).

    ``deleted_receipt_ids``: receipts removed with the entry (never-reviewed
    report).  ``retained_receipt_ids``: receipts detached and KEPT for audit
    (the report has been through approval).
    """

    report: ExpenseReportResponse
    deleted_entry_id: uuid.UUID
    deleted_receipt_ids: list[uuid.UUID]
    retained_receipt_ids: list[uuid.UUID]


class LinkReceiptRequest(BaseModel):
    """POST /expenses/{id}/receipts — attach one of the caller's unlinked receipts."""

    receipt_id: uuid.UUID


class ReceiptLinkResponse(BaseModel):
    """The entry and its report after a receipt was attached (UAT2).

    ``policy_verdict`` is the refreshed pre-submit verdict for THIS entry.
    """

    report: ExpenseReportResponse
    entry: ExpenseEntryResponse
    policy_verdict: PolicyVerdictSummary


class ReceiptDeleteResponse(BaseModel):
    """DELETE /receipts/{id} (UAT2).

    ``outcome``: ``deleted`` (the receipt only; ``expense_id`` is the entry it
    backed, which stays), ``deleted_with_entry`` (the receipt's own auto-drafted
    entry went with it) or ``unshared`` (UAT2 hardening: the receipt was split
    across lines and only line ``expense_id`` let go of it; it still backs the
    others).  ``report``: the affected report after its re-check.
    """

    receipt_id: uuid.UUID
    outcome: Literal["deleted", "deleted_with_entry", "unshared"]
    expense_id: uuid.UUID | None = None
    report: ExpenseReportResponse | None = None
