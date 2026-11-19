"""Pydantic request/response schemas for the receipt module."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ai_agents.receipt_pipeline.correction import InvalidCorrectionError, normalize_correction
from ai_agents.receipt_pipeline.review import receipt_review_status
from backend.app.core.money import AmountConversion
from database.models.receipt import (  # noqa: F401 — re-exported for callers
    OcrStatus,
    ProcessingPath,
)

# Phase 12: the object key of a PNG rendered from an uploaded PDF ends in
# ``<uuid>-from-pdf.png`` (the PDF itself is never stored).
PDF_KEY_MARKER = "-from-pdf"


def is_pdf_rendition_key(key: str | None) -> bool:
    """True when *key* names a PNG rendered from an uploaded PDF."""
    name = (key or "").rsplit("/", 1)[-1]
    return name.endswith(f"{PDF_KEY_MARKER}.png")


class PolicyVerdictSummary(BaseModel):
    """Compact summary of the deterministic policy verdict for an auto-drafted
    expense created from a receipt upload (FEATURE B).

    Surfaced additively on ReceiptResponse so the client can show an at-a-glance
    in/out-of-policy badge; the authoritative flag detail lives on the linked
    ExpenseReport.pre_submit_flags (consumed by analytics + the approval flow).
    """

    in_policy: bool
    blocking_count: int
    advisory_count: int
    flag_types: list[str] = []
    requires_approver: str | None = None
    # UAT2 defect D: why the line looks like a second claim for one purchase (a
    # possible_duplicate, or its receipt matching one that backs / backed another
    # claim) — the server's own flag reason, so the client never says "in policy".
    duplicate_reason: str | None = None


class ReceiptResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    uploaded_by: uuid.UUID
    expense_id: uuid.UUID | None
    image_url: str
    ocr_status: str  # OcrStatus.value
    processing_path: str | None
    extracted_fields: dict[str, Any] | None
    field_confidence: dict[str, Any] | None
    field_provenance: dict[str, Any] | None
    created_at: datetime
    # Populated by the receipt-upload auto-draft path (FEATURE B); None when no
    # draft expense was created (e.g. OCR failed or policy eval degraded).
    policy_verdict: PolicyVerdictSummary | None = None
    # NFR-SEC4: short-lived signed GET link to the receipt image. Set ONLY by
    # GET /receipts/{id}, after RBAC + record scoping. None on POST/PATCH, when
    # there is no image key, or when the provider cannot produce a
    # browser-servable URL. Never logged or persisted. ``image_access_expires_in``
    # is the link lifetime in seconds, so clients can schedule a refresh relative
    # to when they received the response, independent of their own clock skew.
    image_access_url: str | None = None
    image_access_expires_at: datetime | None = None
    image_access_expires_in: int | None = None
    # PATCH (C7-17): whether the correction reached the linked expense entry;
    # POST ?expense_id= (UAT2): "attached".
    linked_expense_sync: LinkedExpenseSync | None = None
    # C7R-18: server-derived review signal (never client-set; recomputed from the
    # persisted receipt state on every read, so a user correction clears its
    # reason). ``review_reasons`` are short human-readable strings, e.g.
    # "Total is not legible — enter the receipt total".
    needs_review: bool = False
    review_reasons: list[str] = []
    # UAT2 hardening: the receipt is evidence a reviewer relied on (part of a
    # submitted / returned report, or kept for audit) and PATCH refuses to change
    # it (409 RECEIPT_LOCKED).  Server-derived on every response.
    correction_locked: bool = False
    correction_locked_reason: str | None = None
    # UAT2 hardening: the OTHER lines (besides ``expense_id``) this receipt backs
    # as an itemized split of one folio across lines of the same report.  The
    # caller's own entry ids; server-derived on every response.
    shared_expense_ids: list[uuid.UUID] = []
    # Product gap 8: the extracted amount in the org's base currency when the
    # receipt is in another one (simulated reference rate, shown with the rate).
    amount_conversion: AmountConversion | None = None
    # Phase 12: "pdf" when the stored image was rendered from an uploaded PDF
    # (server-derived from the object key on every response; the PDF itself is
    # never stored or served).  The three fields below are set ONLY on the
    # POST /receipts response of that upload: the sanitised original file name
    # (display only, never stored) and how many pages the PDF had / the image
    # shows (the first UPLOAD_PDF_MAX_PAGES, stacked).
    source_format: Literal["image", "pdf"] = "image"
    source_filename: str | None = None
    pdf_pages_total: int | None = None
    pdf_pages_used: int | None = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _derive_review(self) -> ReceiptResponse:
        self.needs_review, self.review_reasons = receipt_review_status(
            ocr_status=self.ocr_status,
            extracted_fields=self.extracted_fields,
            field_confidence=self.field_confidence,
            field_provenance=self.field_provenance,
        )
        self.source_format = "pdf" if is_pdf_rendition_key(self.image_url) else "image"
        return self


# C7-16: the correctable fields — exactly the six routed OCR keys
# (services.ocr_provider.interface.RECEIPT_FIELD_KEYS; pinned by a test).
CorrectableReceiptField = Literal[
    "amount", "currency", "date", "vendor", "location", "expense_type"
]

# C7-17: outcome of propagating a correction to the receipt's linked entry.
LinkedExpenseSync = Literal[
    "updated",  # the linked draft entry (and its report total) were updated
    "no_linked_expense",  # the receipt is not linked to an entry
    "not_applicable",  # the field has no entry column (location)
    "unchanged",  # the entry already carried this value
    "drafted",  # C7R-18: the entered total let the upload's deferred draft entry be created
    "skipped_report_not_editable",  # the entry's report is no longer draft
    "skipped_not_owner",  # the entry is not on a report the caller owns
    "attached",  # UAT2: POST ?expense_id= attached the upload to that entry
]


class ReceiptCorrectionRequest(BaseModel):
    """A single-field receipt correction (C7-16: field + value are validated).

    ``value`` is normalized in place (e.g. currency upper-cased, amount rounded
    to cents) by the same validator ``apply_user_correction`` re-runs.
    """

    model_config = ConfigDict(extra="forbid")

    field: CorrectableReceiptField
    value: Any

    @model_validator(mode="after")
    def _validate_value(self) -> ReceiptCorrectionRequest:
        try:
            self.value = normalize_correction(self.field, self.value)
        except InvalidCorrectionError as exc:
            raise ValueError(str(exc)) from exc
        return self
