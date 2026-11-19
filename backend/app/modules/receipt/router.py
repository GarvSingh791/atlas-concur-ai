"""Receipt module router — receipt upload, read + correction endpoints.

Mounted at the API v1 root (no prefix) so paths match the design docs literally:
  POST  /api/v1/receipts                  (?expense_id= attaches to a draft entry)
  GET   /api/v1/receipts                  (caller's attachable receipts, UAT2)
  GET   /api/v1/receipts/{receipt_id}
  PATCH /api/v1/receipts/{receipt_id}
  DELETE /api/v1/receipts/{receipt_id}    (UAT2)

GET is readable by the uploader (the owner of the receipt's report), the owner's
direct manager and finance_admin (``scope_query`` on ``Receipt.uploaded_by``);
it carries a short-lived signed image link (NFR-SEC4). POST/PATCH stay
uploader-only: POST requires ``expense.create.own`` (it auto-drafts an expense —
C7-19) and PATCH requires ``expense.update.own`` (it can edit the linked entry —
C7-17).

Thin router — logic follows Dev Rule 5 (thin router, no business logic here beyond
orchestration of pipeline calls and idempotency).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import urlparse

import sqlalchemy as sa
from fastapi import APIRouter, Depends, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent import entry_context
from ai_agents.agents.expense_agent.categorize import category_for_expense_type
from ai_agents.agents.expense_agent.presubmit import (
    ESCALATING_FLAG_TYPES,
    POSSIBLE_DUPLICATE,
    RECEIPT_MISMATCH,
    build_eval_item_from_fields,
    build_flags_snapshot,
    draft_entries_for_expenses,
    receipt_support_issue,
    verdicts_to_flags,
)
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline import (
    RECEIPT_INBOX_TITLE,
    apply_user_correction,
    run_receipt_pipeline,
)
from ai_agents.receipt_pipeline.correction import (
    InvalidCorrectionError,
    ReceiptLockedError,
    ReceiptNotFoundError,
)
from ai_agents.receipt_pipeline.evidence import backing_expense_ids, shared_expense_ids
from ai_agents.receipt_pipeline.retention import (
    RECEIPT_LOCKED,
    RECEIPT_LOCKED_MESSAGE,
    correction_locked_receipt_ids,
)
from ai_agents.tools.policy_eval import PolicyEvalTool
from backend.app.core.audit import record_audit
from backend.app.core.concurrency import if_match_version
from backend.app.core.database import get_db
from backend.app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    UnprocessableEntityError,
)
from backend.app.core.idempotency import (
    idempotency_key_header,
    idempotency_lookup,
    idempotency_record,
)
from backend.app.core.money import amount_conversion
from backend.app.core.rbac import require_permission, scope_query
from backend.app.modules.ai.runtime import build_agent_context
from backend.app.modules.expense import service as expense_service
from backend.app.modules.expense.policy_fields import (
    MILEAGE_COMPUTED_COLUMNS,
    RECEIPT_ON_MILEAGE_MESSAGE,
    clear_unfit_policy_fields,
    is_mileage_entry,
)
from backend.app.modules.expense.schemas import ReceiptDeleteResponse
from backend.app.modules.expense.service import _compute_total
from backend.app.modules.receipt.quota import STORED_BYTES_KEY, check_upload_quota
from backend.app.modules.receipt.schemas import (
    PDF_KEY_MARKER,
    LinkedExpenseSync,
    PolicyVerdictSummary,
    ReceiptCorrectionRequest,
    ReceiptResponse,
)
from backend.app.modules.receipt.upload_guard import (
    KIND_PDF_SOURCE,
    inspect_upload,
    read_upload_bounded,
    safe_filename,
)
from config.constants.fx_rates import UnknownCurrency, normalize_currency
from config.constants.permissions import (
    EXPENSE_CREATE_OWN,
    EXPENSE_READ_OWN,
    EXPENSE_UPDATE_OWN,
)
from config.settings import get_settings
from database.models.approval import Approval, ApprovalSubjectType
from database.models.expense import (
    Expense,
    ExpenseCategory,
    ExpenseMatchSource,
    ExpenseStatus,
)
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, User
from database.models.receipt import OcrStatus, Receipt
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrProvider
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import StorageProvider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["receipts"])

# Title of the auto-created draft report that collects receipt-upload expenses.
# Single source of truth lives with the inbox helpers (C7-22 smart-match).
_RECEIPT_INBOX_TITLE = RECEIPT_INBOX_TITLE

# NFR-SEC4: hard ceiling on a receipt-image link's lifetime, regardless of the
# STORAGE_PRESIGNED_URL_EXPIRY_SECONDS setting.
_MAX_IMAGE_URL_TTL_SECONDS = 300

# Only these schemes are browser-servable. LocalStorageProvider returns file://
# URLs, which a browser cannot load and which would leak a server filesystem path.
_SERVABLE_URL_SCHEMES = frozenset({"https", "http"})


def _coerce_amount(value: object) -> Decimal | None:
    """Coerce an OCR-extracted amount into a non-negative Decimal, or None."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amount < 0:
        return None
    return amount


# C7R: provenances whose amount may seed a draft entry. 'ai_inferred' is NOT one
# of them: the gap-fill LLM never sees the image, so an amount it inferred (from
# the itinerary) is a suggestion, not a reading — the user must enter the total.
_DRAFTABLE_AMOUNT_PROVENANCE = frozenset({"deterministic", "user_edited"})

# UAT2 defect D: flags only the report-wide pre-submit check produces (it compares
# a line with the owner's other lines and receipts) — never part of the
# auto-draft's single-entry verdict, so they are added to the upload response.
_REPORT_WIDE_FLAG_TYPES = frozenset({POSSIBLE_DUPLICATE, RECEIPT_MISMATCH})


def _draftable_amount(receipt: Receipt) -> Decimal | None:
    """The receipt amount an expense entry may be drafted from, or None (C7R).

    Requires a numeric amount that OCR read with confidence ('deterministic') or
    the user entered ('user_edited').  A withheld (null) total and an LLM-guessed
    ('ai_inferred') total both return None: no draft, no policy verdict.
    """
    fields = receipt.extracted_fields or {}
    amount = _coerce_amount(fields.get("amount"))
    if amount is None:
        return None
    provenance = (receipt.field_provenance or {}).get("amount")
    if provenance not in _DRAFTABLE_AMOUNT_PROVENANCE:
        return None
    if _receipt_currency(fields) is _UNSUPPORTED_CURRENCY:
        # Product gap 8: an amount in a currency Atlas cannot convert is never
        # drafted (it could not be compared with a cap or added to a total); the
        # user picks a supported currency in review, which drafts it then.
        return None
    return amount


_UNSUPPORTED_CURRENCY = object()


def _receipt_currency(fields: dict) -> str | object | None:
    """The receipt's currency: a supported code, None (not read) or the sentinel."""
    raw = fields.get("currency")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return normalize_currency(raw)
    except UnknownCurrency:
        return _UNSUPPORTED_CURRENCY


def _entry_receipt_issue(
    db: Session,
    *,
    entry: Expense,
    agent_ctx: object,
    owner_id: uuid.UUID,
    receipt: Receipt | None = None,
) -> str | None:
    """Why *entry*'s receipt does not satisfy RECEIPT-REQ (None = it does) (C7R).

    The SAME ``receipt_support_issue`` check approval time and /policy/evaluate
    use, so the auto-draft never reports "in policy" for an amount the claimant
    typed (user_edited is not evidence) or one the receipt does not cover.
    Fails closed: an unexpected lookup error is reported as unverifiable.
    """
    if not isinstance(agent_ctx, AgentContext):
        return "the attached receipt could not be verified"
    try:
        db.flush()  # the receipt -> entry link must be visible to the lookup
        (draft,) = draft_entries_for_expenses(
            [entry], session=db, organization_id=entry.organization_id
        )
        if receipt is not None:
            # The receipt being processed IS the entry's evidence (the lookup picks
            # the first attached receipt; in this flow it is the only one).
            draft = dataclasses.replace(draft, primary_receipt_id=str(receipt.id))
        return receipt_support_issue(draft, context=agent_ctx, session=db, owner_id=owner_id)
    except Exception:  # noqa: BLE001 — fail closed, never in-policy by accident
        logger.warning("receipt: receipt-support check failed for expense=%s", entry.id)
        return "the attached receipt could not be verified"


def _parse_expense_date(value: object) -> _date | None:
    """Parse an OCR-extracted ISO date string into a date, or None."""
    if isinstance(value, _date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _date.fromisoformat(value[:10])
    except ValueError:
        return None


def _find_or_create_receipt_inbox(db: Session, user: User) -> ExpenseReport:
    """Find the caller's draft 'Receipt Inbox' report, or create one (flush-only).

    C7R: a report sent back for changes returns to draft (P12) but it is under
    review — new uploads must not land on it.  Only an inbox that has never been
    through an approval is reused, and the newest one wins deterministically.

    The reused inbox is read ``FOR UPDATE`` (C7R review): the caller then adds an
    entry and rewrites the total, so a concurrent submit of the same inbox waits
    for this transaction and submits the report WITH the new entry; if the submit
    committed first, the re-checked ``status == draft`` no longer matches and a
    fresh inbox is created.  Without the lock the entry could commit onto a
    report already submitted (or auto-approved) that the evaluation never saw.
    """
    reviewed = sa.select(Approval.id).where(
        Approval.subject_type == ApprovalSubjectType.expense_report,
        Approval.subject_id == ExpenseReport.id,
    )
    report = db.scalar(
        sa.select(ExpenseReport)
        .where(
            ExpenseReport.organization_id == user.organization_id,
            ExpenseReport.owner_id == user.id,
            ExpenseReport.title == _RECEIPT_INBOX_TITLE,
            ExpenseReport.status == ExpenseReportStatus.draft,
            ~reviewed.exists(),
        )
        .order_by(ExpenseReport.created_at.desc(), ExpenseReport.id.desc())
        .limit(1)
        .with_for_update(of=ExpenseReport)
        .execution_options(populate_existing=True)
    )
    if report is not None:
        return report
    report = ExpenseReport(
        organization_id=user.organization_id,
        owner_id=user.id,
        trip_id=None,
        title=_RECEIPT_INBOX_TITLE,
        status=ExpenseReportStatus.draft,
        total_amount=Decimal("0"),
        currency=user.organization.base_currency if user.organization else "USD",
        pre_submit_flags=None,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(report)
    db.flush()
    return report


def _auto_draft_expense_from_receipt(
    db: Session,
    *,
    user: User,
    receipt: Receipt,
    agent_ctx: object,
) -> PolicyVerdictSummary | None:
    """FEATURE B: turn a processed receipt into a draft Expense + policy verdict.

    Runs inside the caller's existing transaction (flush-only; no commit). Only
    fires when OCR succeeded and a numeric amount was extracted. Persists the
    policy flags onto the parent report's pre_submit_flags (the same snapshot the
    analytics + approval flow consume) and links receipt.expense_id.

    Returns a PolicyVerdictSummary for the response, or None when no draft was
    created. Any failure here is swallowed by the caller so the upload still
    succeeds (degrade, never block).

    C7R-18: a receipt whose total was WITHHELD (illegible / ambiguous) has a null
    amount, so NO draft and NO policy verdict are produced — an unknown amount is
    never drafted or reported in-policy. The draft is created when the user
    enters the total (PATCH amount, see ``correct_receipt``).  The same holds for
    a total the gap-fill LLM inferred (``_draftable_amount``).

    C7R: RECEIPT-REQ uses ``receipt_support_issue`` (as approval time does), so a
    total the user typed is not treated as receipt evidence: the verdict says
    ``missing_receipt`` instead of "in policy".
    """
    if receipt.ocr_status != OcrStatus.processed:
        return None

    fields = receipt.extracted_fields or {}
    # C7R: only an OCR-read or user-entered total is drafted; an LLM-inferred one
    # is a suggestion the UI withholds, so drafting it would claim a guess.
    amount = _draftable_amount(receipt)
    if amount is None:
        return None

    # C7-21: normalized lookup; "transportation" (the OCR prompt's own value)
    # maps to ground_transport instead of falling through to "other".
    category_value = category_for_expense_type(fields.get("expense_type")) or "other"
    category = ExpenseCategory(category_value)

    # Product gap 8: a receipt with no currency is in the org's base currency;
    # an unsupported one never reaches here (``_draftable_amount``).
    base_currency = user.organization.base_currency if user.organization else "USD"
    currency = _receipt_currency(fields)
    currency_code = currency if isinstance(currency, str) else base_currency

    expense_date = _parse_expense_date(fields.get("date")) or _date.today()
    vendor = fields.get("vendor")
    vendor_str = vendor if isinstance(vendor, str) and vendor else None

    # 1. Find-or-create the draft "Receipt Inbox" report.
    report = _find_or_create_receipt_inbox(db, user)

    # 2. Create the draft expense entry and link the receipt.
    expense = Expense(
        organization_id=user.organization_id,
        report_id=report.id,
        trip_segment_id=None,
        category=category,
        amount=amount,
        currency=currency_code,
        expense_date=expense_date,
        vendor=vendor_str,
        description=None,
        status=ExpenseStatus.draft,
        match_source=ExpenseMatchSource.receipt,
        is_itemized=False,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(expense)
    db.flush()
    receipt.expense_id = expense.id

    # 3. Recompute the report total from its entries (DERIVED, never client-set),
    #    in the report's currency (product gap 8: a foreign entry is converted).
    report.total_amount = _compute_total(db, report.id)
    report.updated_by = user.id

    # 4. Run the deterministic policy engine on this single entry.
    ref_id = vendor_str or str(expense.id)
    item = build_eval_item_from_fields(
        # Review round 3: the receipt's own total / words ride along (partial claims,
        # trusted ground-transport classification).
        entry_context.with_receipt_evidence(
            fields, receipt.field_provenance or {}, grounding_context=receipt.grounding_context
        ),
        category=category_value,
        ref_id=ref_id,
        provenance=receipt.field_provenance or {},
    )
    verdict = PolicyEvalTool().evaluate(item, context=agent_ctx, session=db)  # type: ignore[arg-type]

    # 5. Map verdicts → flags via the reusable presubmit helper.
    receipt_issue = _entry_receipt_issue(
        db, entry=expense, agent_ctx=agent_ctx, owner_id=user.id, receipt=receipt
    )
    flags, _failing = verdicts_to_flags(
        list(verdict.verdicts),
        entry_ref=ref_id,
        has_receipt=receipt_issue is None,
        category=category_value,
        receipt_issue=receipt_issue,
        entry_id=str(expense.id),
    )
    # Accumulate flags across ALL entries in the (shared) Receipt Inbox report so a
    # later in-policy upload cannot wipe an earlier violation. The response summary
    # reflects THIS receipt; the persisted report snapshot reflects every entry.
    new_snapshot = build_flags_snapshot(flags, entry_count=1)
    # Reflect a blocking violation on the entry's own status so the per-entry policy
    # badge (driven by Expense.status) matches the report's pre-submit check panel.
    if int(new_snapshot.get("blocking_count", 0)) > 0:
        expense.status = ExpenseStatus.flagged
    existing = report.pre_submit_flags or {}
    merged_flags = list(existing.get("flags") or []) + list(new_snapshot.get("flags") or [])
    entry_count = int(
        db.scalar(sa.select(sa.func.count(Expense.id)).where(Expense.report_id == report.id)) or 1
    )
    report.pre_submit_flags = {
        "flags": merged_flags,
        "evaluated_entry_count": entry_count,
        "blocking_count": int(existing.get("blocking_count", 0))
        + int(new_snapshot.get("blocking_count", 0)),
        "advisory_count": int(existing.get("advisory_count", 0))
        + int(new_snapshot.get("advisory_count", 0)),
    }
    db.flush()

    # 6. UAT2 defect D: the merge above only knows THIS line.  The report-wide
    #    pre-submit check — the one that compares lines (a re-uploaded receipt is a
    #    possible_duplicate of its twin, a reused receipt a receipt_mismatch) — is
    #    re-run ONCE over the inbox, its version bumped (a stale inbox tab gets a
    #    409), and any other draft holding a twin of the new line is re-checked.
    #    On a refresh failure the merged snapshot is kept plus a not_evaluated note.
    _refresh_report_checks(db, report=report, entry=expense, actor_id=user.id)
    # The response summary is this line's single-entry verdict plus what only the
    # report-wide check can see (evidence flags, a not-re-evaluated note).
    extra = [
        f
        for f in expense_service.entry_flags(report, expense.id)
        if f.get("flag_type") in _REPORT_WIDE_FLAG_TYPES
        or (f.get("flag_type") == "not_evaluated" and f.get("entry_id") is None)
    ]
    extra_blocking = sum(1 for f in extra if f.get("severity") == "blocking")
    extra_advisory = sum(1 for f in extra if f.get("severity") == "advisory")

    requires_approver = next((f.requires_approver for f in flags if f.requires_approver), None)
    return PolicyVerdictSummary(
        # A blocking flag (e.g. missing_receipt, which is not a rule verdict) means
        # the entry is NOT in policy, whatever the rule verdicts alone say.
        # UAT2: an advisory-but-escalating flag (receipt total not verifiable) is
        # not "in policy" either — a human will review it.
        in_policy=verdict.in_policy
        and int(new_snapshot["blocking_count"]) == 0
        and not any(f.flag_type in ESCALATING_FLAG_TYPES for f in flags)
        and not extra,
        blocking_count=int(new_snapshot["blocking_count"]) + extra_blocking,
        advisory_count=int(new_snapshot["advisory_count"]) + extra_advisory,
        flag_types=sorted({f.flag_type for f in flags} | {str(f["flag_type"]) for f in extra}),
        requires_approver=requires_approver,
        duplicate_reason=expense_service.duplicate_reason(extra),
    )


def _image_url_ttl() -> int:
    """Return the image-link TTL in seconds: the configured value, capped at 300 s."""
    configured = get_settings().STORAGE_PRESIGNED_URL_EXPIRY_SECONDS
    return max(1, min(configured, _MAX_IMAGE_URL_TTL_SECONDS))


def _attach_image_access(
    response: ReceiptResponse, receipt: Receipt, storage: StorageProvider
) -> None:
    """Attach a short-lived signed image link to *response* (NFR-SEC4).

    MUST only be called after RBAC + record scoping have admitted the caller.
    Degrades to ``None`` (never an error) when there is no image key, the
    provider cannot sign, or the URL is not browser-servable. The URL itself is
    never logged; the warning carries only the receipt id.
    """
    if not receipt.image_url:
        return
    ttl = _image_url_ttl()
    # Taken BEFORE signing, so the reported expiry is never later than the real one.
    issued_at = datetime.now(timezone.utc)
    try:
        signed = storage.generate_presigned_url(receipt.image_url, expires_in=ttl)
    except Exception as exc:  # noqa: BLE001 - any signing failure degrades to "no link"
        # ProviderError, or an unwrapped SDK/client-setup error from a real
        # provider: never 500 a metadata read over an optional image link. Log
        # only the receipt id and the exception type — never the message, which
        # could carry the key or endpoint, and never a URL.
        logger.warning(
            "receipt.get: image link unavailable for receipt=%s (%s)",
            receipt.id,
            type(exc).__name__,
        )
        return
    if urlparse(signed.url).scheme not in _SERVABLE_URL_SCHEMES:
        return
    lifetime = min(signed.expires_in, ttl)
    response.image_access_url = signed.url
    response.image_access_expires_at = issued_at + timedelta(seconds=lifetime)
    response.image_access_expires_in = lifetime


def _receipt_responses(db: Session, receipts: list[Receipt]) -> list[ReceiptResponse]:
    """Serialize *receipts* with their server-derived correction lock (UAT2 hardening)."""
    locked = (
        correction_locked_receipt_ids(
            db, organization_id=receipts[0].organization_id, receipts=receipts
        )
        if receipts
        else set()
    )
    out: list[ReceiptResponse] = []
    base = (
        db.scalar(
            sa.select(Organization.base_currency).where(
                Organization.id == receipts[0].organization_id
            )
        )
        if receipts
        else None
    )
    for receipt in receipts:
        response = ReceiptResponse.model_validate(receipt)
        response.shared_expense_ids = shared_expense_ids(receipt)
        fields = receipt.extracted_fields if isinstance(receipt.extracted_fields, dict) else {}
        # Product gap 8: original + base-currency amount for the review screen.
        response.amount_conversion = amount_conversion(
            fields.get("amount"), fields.get("currency"), base
        )
        if receipt.id in locked:
            response.correction_locked = True
            response.correction_locked_reason = RECEIPT_LOCKED_MESSAGE
        out.append(response)
    return out


def _receipt_response(db: Session, receipt: Receipt) -> ReceiptResponse:
    return _receipt_responses(db, [receipt])[0]


@router.post("/receipts", response_model=ReceiptResponse, status_code=201)
async def upload_receipt(
    file: UploadFile,
    # C7-19: uploading auto-drafts an expense, so it needs the same permission
    # as POST /expense-reports (finance_admin, which lacks it, gets 403 and
    # nothing is stored, OCR'd or drafted — the dependency runs first).
    user: Annotated[User, Depends(require_permission(EXPENSE_CREATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    idem_key: Annotated[str | None, Depends(idempotency_key_header)],
    ocr: Annotated[OcrProvider, Depends(get_ocr_provider)],
    llm: Annotated[LlmProvider, Depends(get_llm_provider)],
    storage: Annotated[StorageProvider, Depends(get_storage_provider)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
    expense_id: uuid.UUID | None = None,
) -> ReceiptResponse:
    """Upload a receipt image, run OCR pipeline, and return the extracted receipt.

    Stores the image in object storage, creates a Receipt row (pending), runs
    the full OCR + gap-fill pipeline, writes an audit row, and commits atomically.
    Supports idempotent retries via the Idempotency-Key header.

    ``expense_id`` (UAT2 attach): the receipt is attached to that entry of the
    caller's DRAFT report.  The row is inserted UNLINKED and OCR runs without any
    lock on the entry or report; only then is the link set in one short locked
    step (``expense_service.link_receipt_locked``: report → entry → receipt,
    draft status and the optional If-Match REPORT version re-checked), the
    pre-submit check re-run, the version bumped and the attach audited.  A
    refusal stores nothing (row removed, image deleted best effort).

    Phase 11: the file is size-capped (413), typed by its content against an
    allow-list (415 on a mismatch with the claimed type / extension), checked
    structurally and stripped of metadata (422 when empty / corrupt / a
    decompression bomb), and counted against the storage quota (409
    UPLOAD_QUOTA_EXCEEDED) — all before it is stored or sent to OCR.
    """
    # 1. Idempotency lookup — replay if already processed
    if idem_key is not None:
        existing_id = idempotency_lookup(
            db,
            organization_id=user.organization_id,
            scope="receipt.upload",
            key=idem_key,
        )
        if existing_id is not None:
            existing = db.scalar(
                sa.select(Receipt).where(
                    Receipt.id == existing_id,
                    Receipt.organization_id == user.organization_id,
                )
            )
            if existing is None or existing.uploaded_by != user.id:
                # Keys are org-scoped; a key first used by ANOTHER user (or whose
                # record is gone) must never replay — i.e. disclose — that
                # receipt, nor fall through to a duplicate-key insert.
                raise ConflictError("Idempotency key has already been used for another request.")
            return _receipt_response(db, existing)

    # 2. Validate expense_id BEFORE storing anything — prevent IDOR across
    #    expense records, and refuse attaching evidence to an entry whose report
    #    is no longer a draft: a receipt attached after submit would silently
    #    satisfy RECEIPT-REQ at approval-time re-evaluation (C7 review).
    if expense_id is not None:
        report_row = db.execute(
            sa.select(ExpenseReport.status, ExpenseReport.withdrawn_at)
            .join(Expense, ExpenseReport.id == Expense.report_id)
            .where(
                Expense.id == expense_id,
                Expense.organization_id == user.organization_id,
                ExpenseReport.organization_id == user.organization_id,
                ExpenseReport.owner_id == user.id,
            )
        ).first()
        if report_row is None:
            raise NotFoundError("Expense not found or not accessible.")
        report_status, withdrawn_at = report_row
        if withdrawn_at is not None:
            # Product gap 4: a withdrawn report is closed (read-only).
            raise ConflictError(
                "Receipts can't be attached to entries of a withdrawn expense report.",
                code="REPORT_WITHDRAWN",
            )
        if report_status != ExpenseReportStatus.draft:
            raise ConflictError(
                "Receipts can only be attached to entries of a draft expense report "
                f"(this report is '{report_status.value}')."
            )
        # Product gap 2 (REF-08): refused before OCR runs or anything is stored.
        target_category = db.scalar(
            sa.select(Expense.category).where(
                Expense.id == expense_id, Expense.organization_id == user.organization_id
            )
        )
        if target_category == ExpenseCategory.mileage:
            raise UnprocessableEntityError(RECEIPT_ON_MILEAGE_MESSAGE)
        if expected_version is not None:
            current_version = db.scalar(
                sa.select(ExpenseReport.version)
                .join(Expense, ExpenseReport.id == Expense.report_id)
                .where(Expense.id == expense_id)
            )
            if current_version != expected_version:
                raise ConflictError("Expense report was modified by another request.")

    # 3. Phase 11: read (bounded) and validate the file BEFORE anything is stored
    #    or sent to OCR — type sniffed from the content against an allow-list,
    #    structure / decompression-bomb checks, metadata (EXIF/GPS) stripped —
    #    then the per-user / per-org storage quota.
    raw = await read_upload_bounded(file, get_settings().MAX_UPLOAD_BYTES)
    checked = await run_in_threadpool(
        inspect_upload, raw, filename=file.filename, content_type=file.content_type
    )
    del raw
    check_upload_quota(db, user, len(checked.data))

    # 3b. Store the SANITISED bytes under a server-generated key (the client file
    #     name is never used) with the SNIFFED content type.
    #     Phase 12: a PDF upload stores ONLY the PNG rendered from it; its key
    #     carries a ``-from-pdf`` marker (still ``.png``) so every later read
    #     can say "from PDF" without a schema change.
    marker = PDF_KEY_MARKER if checked.source == KIND_PDF_SOURCE else ""
    object_key = f"{user.organization_id}/{uuid.uuid4()}{marker}.{checked.extension}"
    storage.put_object(object_key, checked.data, content_type=checked.content_type)
    image_url = object_key  # store the key; presigned URLs generated on demand

    # 4. Create Receipt row — UNLINKED even for an attach (UAT2): a row carrying
    #    expense_id would hold a FOR KEY SHARE lock on the entry for the whole OCR
    #    call and deadlock with an entry edit / delete (report → entry order).
    receipt = Receipt(
        organization_id=user.organization_id,
        uploaded_by=user.id,
        expense_id=None,
        image_url=image_url,
        ocr_status=OcrStatus.pending,
    )
    db.add(receipt)
    db.flush()

    # 5. Build agent context
    agent_ctx = build_agent_context(user, db)

    # 6. Run OCR + gap-fill pipeline (flush-only; we own the commit).
    #    The pipeline is synchronous and, with a real OCR/LLM provider, makes
    #    blocking HTTP calls (up to OCR_TIMEOUT_SECONDS per attempt, with a retry
    #    and a fallback model). Run it in the threadpool so it never stalls the
    #    event loop (other requests, /health) on a single-worker deploy. The
    #    session is not used concurrently — this coroutine awaits the call.
    await run_in_threadpool(
        run_receipt_pipeline,
        db,
        context=agent_ctx,
        receipt_id=receipt.id,
        ocr=ocr,
        llm=llm,
        storage=storage,
    )

    # 6b. FEATURE B — auto-draft an expense + run the policy verdict.
    #
    # Only when the upload did NOT target an existing expense (a fresh, unlinked
    # receipt). Runs inside this same transaction via a SAVEPOINT so any
    # policy/expense failure rolls back ONLY the draft work — the receipt + its
    # extracted fields survive and the upload still returns 201 (degrade, never
    # block).
    policy_verdict: PolicyVerdictSummary | None = None
    sync: LinkedExpenseSync | None = None
    if expense_id is not None:
        try:
            _report, _entry, policy_verdict = expense_service.link_receipt_locked(
                db,
                user,
                receipt=receipt,
                entry_id=expense_id,
                expected_version=expected_version,
                source="upload",
            )
        except AppError:
            # Refused under the lock (submitted while OCR ran, stale version, ...):
            # nothing of this upload may survive.  get_db rolls the transaction
            # back; the row is removed explicitly too, then the stored image.
            db.delete(receipt)
            db.flush()
            expense_service.purge_receipt_objects(storage, [object_key])
            raise
        sync = "attached"
    else:
        try:
            with db.begin_nested():
                policy_verdict = _auto_draft_expense_from_receipt(
                    db, user=user, receipt=receipt, agent_ctx=agent_ctx
                )
        except Exception:  # noqa: BLE001 — degrade gracefully; upload must still succeed
            policy_verdict = None
            logger.exception(
                "receipt.upload: auto-draft expense/policy step failed for receipt=%s",
                receipt.id,
            )

    # 7. Write audit row
    record_audit(
        db,
        organization_id=user.organization_id,
        action="receipt.processed",
        entity_type="receipt",
        entity_id=receipt.id,
        actor_id=user.id,
        # Phase 11: the stored size feeds the storage quota (receipt/quota.py).
        after_state={
            STORED_BYTES_KEY: len(checked.data),
            "content_type": checked.content_type,
            # Phase 12: a rasterised PDF (page counts only; never the file name).
            **(
                {
                    "source_format": KIND_PDF_SOURCE,
                    "pdf_pages_total": checked.pages_total,
                    "pdf_pages_used": checked.pages_used,
                }
                if checked.source == KIND_PDF_SOURCE
                else {}
            ),
        },
    )

    # 8. Record idempotency key (before commit so it lands atomically)
    if idem_key is not None:
        idempotency_record(
            db,
            organization_id=user.organization_id,
            scope="receipt.upload",
            key=idem_key,
            entity_id=receipt.id,
        )

    # 9. Single commit for entity + audit + idempotency key (+ auto-draft expense)
    db.commit()

    # 10. Refresh and return (attach the transient policy verdict for the response)
    db.refresh(receipt)
    response = _receipt_response(db, receipt)
    response.policy_verdict = policy_verdict
    response.linked_expense_sync = sync
    if checked.source == KIND_PDF_SOURCE:
        # Phase 12: transient, display only (never stored or logged).
        response.source_filename = safe_filename(file.filename)
        response.pdf_pages_total = checked.pages_total
        response.pdf_pages_used = checked.pages_used
    return response


@router.get("/receipts", response_model=list[ReceiptResponse])
def list_attachable_receipts(
    user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    for_expense_id: uuid.UUID | None = None,
) -> list[ReceiptResponse]:
    """The caller's own receipts that can be attached to an expense entry (UAT2).

    Unlinked, already read by OCR, not kept for audit; newest first, at most 50.
    Owner-only (``uploaded_by``) — a manager never sees a report's receipts here.
    ``for_expense_id`` (UAT2 hardening): also offer receipts on OTHER lines of that
    entry's draft report, so one itemized receipt can be split across lines.
    """
    return _receipt_responses(
        db, expense_service.list_attachable_receipts(db, user, for_expense_id=for_expense_id)
    )


@router.delete("/receipts/{receipt_id}", response_model=ReceiptDeleteResponse)
def delete_receipt(
    receipt_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageProvider, Depends(get_storage_provider)],
    discard_drafted_entry: bool = False,
    from_expense_id: uuid.UUID | None = None,
) -> ReceiptDeleteResponse:
    """Delete one of the caller's receipts (UAT2).

    404 unreadable, 403 not the uploader.  409 RECEIPT_RETAINED when it is or was
    evidence on a report that has been through approval ("kept for audit"), 409
    RECEIPT_IN_USE when it backs an entry the caller cannot change.  A receipt on
    a never-submitted draft is removed and that report re-checked;
    ``discard_drafted_entry=true`` also removes the entry the upload auto-drafted
    from it (undoing the upload).  ``from_expense_id`` (UAT2 hardening): when the
    receipt is split across several lines, only that line lets go of it.  The
    stored image is deleted after the commit.
    """
    response, keys = expense_service.delete_receipt(
        db,
        user,
        receipt_id,
        discard_drafted_entry=discard_drafted_entry,
        from_expense_id=from_expense_id,
    )
    expense_service.purge_receipt_objects(storage, keys)
    return response


@router.get("/receipts/{receipt_id}", response_model=ReceiptResponse)
def get_receipt(
    receipt_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission(EXPENSE_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageProvider, Depends(get_storage_provider)],
) -> ReceiptResponse:
    """Fetch a single receipt plus a short-lived signed image link (NFR-SEC4).

    Gated by ``expense.read.own`` (held by every role) and narrowed by
    ``scope_query`` on ``Receipt.uploaded_by``: the uploader (who is always the
    owner of the receipt's report), the uploader's direct manager, and
    finance_admin org-wide. A receipt that does not exist, belongs to another
    tenant, or is out of the caller's scope returns 404 — no existence leak.

    The image link is minted only after scoping, expires within 300 s, and is
    ``None`` when the provider cannot produce a browser-servable URL.
    """
    stmt = scope_query(
        sa.select(Receipt).where(Receipt.id == receipt_id),
        user=user,
        ownership_column=Receipt.uploaded_by,
        org_column=Receipt.organization_id,
        session=db,
    )
    receipt = db.scalars(stmt).first()
    if receipt is None:
        raise NotFoundError("Receipt not found or not accessible.")
    response = _receipt_response(db, receipt)
    _attach_image_access(response, receipt, storage)
    return response


# C7-17: receipt field → Expense column. "location" has no entry column.
_ENTRY_FIELD_FOR_RECEIPT_FIELD: dict[str, str] = {
    "amount": "amount",
    "currency": "currency",
    "date": "expense_date",
    "vendor": "vendor",
    "expense_type": "category",
}


def _entry_value_for(field: str, value: object) -> object:
    """Convert a normalized receipt correction value to the Expense column value."""
    if field == "amount":
        return Decimal(str(value)).quantize(Decimal("0.01"))
    if field == "date":
        return _date.fromisoformat(str(value))
    if field == "expense_type":
        return ExpenseCategory(category_for_expense_type(value) or "other")
    return value


def _entry_audit_snapshot(entry: Expense) -> dict:
    """Same shape as the expense service's ``expense.updated`` snapshot, plus vendor."""
    category = entry.category
    status = entry.status
    return {
        "report_id": str(entry.report_id),
        "category": category.value if isinstance(category, ExpenseCategory) else category,
        "amount": str(entry.amount),
        "currency": entry.currency,
        "expense_date": str(entry.expense_date),
        "status": status.value if isinstance(status, ExpenseStatus) else status,
        "is_itemized": entry.is_itemized,
        "vendor": entry.vendor,
        # Product gap 2: a category correction may clear these.
        "attendee_count": entry.attendee_count,
        "disclosure": entry.disclosure,
        "mileage": entry.mileage,
    }


def _flag_ref(entry: Expense) -> str:
    """The pre_submit_flags entry_ref the auto-draft uses for *entry*."""
    return entry.vendor or str(entry.id)


def _refresh_report_checks(
    db: Session, *, report: ExpenseReport, entry: Expense, actor_id: uuid.UUID
) -> None:
    """Re-run *report*'s pre-submit check after *entry* (or its receipt) changed.

    Bumps the report version (a stale tab gets a 409) and re-checks the owner's
    other drafts that carry a duplicate flag or hold a twin of *entry*.  Flush-only.
    """
    expense_service.refresh_presubmit_flags(db, report)
    expense_service._bump_report_version(db, report, expected_version=None, actor_id=actor_id)
    expense_service.refresh_duplicate_peers(
        db, report=report, exclude_ids={report.id}, actor_id=actor_id, twins_of=[entry]
    )


def _refresh_entry_policy(
    db: Session,
    *,
    agent_ctx: object,
    receipt: Receipt,
    entry: Expense,
    report: ExpenseReport,
    old_ref: str,
) -> None:
    """C7-17: re-run the auto-draft's single-entry policy check after a correction.

    A corrected amount/category/vendor/date invalidates the upload-time verdict:
    the entry's flagged status and the report's pre_submit_flags must follow the
    corrected values (a $900 meal corrected to $20 loses its blocking flag; a $20
    receipt corrected to $4,999 gains one). Only receipt-sourced (auto-drafted)
    entries on the Receipt Inbox are refreshed; the submit path re-evaluates
    every entry anyway.

    Flags are keyed by vendor (or entry id), so every receipt-sourced entry of the
    report sharing the old or new ref is re-evaluated together — a shared vendor
    key cannot be attributed to a single entry. Flush-only.
    """
    new_ref = _flag_ref(entry)
    refs = {old_ref, new_ref, str(entry.id)}
    affected = [
        e
        for e in db.scalars(
            sa.select(Expense).where(
                Expense.report_id == report.id,
                Expense.match_source == ExpenseMatchSource.receipt,
            )
        ).all()
        if e.id == entry.id or _flag_ref(e) in refs
    ]

    new_flags: list[dict] = []
    for e in affected:
        if e.id == entry.id:
            fields = dict(receipt.extracted_fields or {})
            provenance = receipt.field_provenance or {}
        else:
            sibling_receipt = db.scalar(
                sa.select(Receipt).where(
                    Receipt.expense_id == e.id,
                    Receipt.organization_id == report.organization_id,
                )
            )
            fields = dict((sibling_receipt.extracted_fields or {}) if sibling_receipt else {})
            provenance = (sibling_receipt.field_provenance or {}) if sibling_receipt else {}
        # What the RECEIPT read (total, words) is captured before the entry's claimed
        # amount / typed vendor overwrite it (review round 3).
        backing = receipt if e.id == entry.id else sibling_receipt
        fields = entry_context.with_receipt_evidence(
            fields,
            provenance,
            shared=backing is not None and len(backing_expense_ids(backing)) > 1,
            grounding_context=backing.grounding_context if backing is not None else None,
        )
        # The entry columns are authoritative for the fields they carry.
        fields["amount"] = float(Decimal(str(e.amount)))
        fields["vendor"] = e.vendor
        category_value = e.category.value if isinstance(e.category, ExpenseCategory) else e.category
        ref = _flag_ref(e)
        item = build_eval_item_from_fields(
            fields, category=category_value, ref_id=ref, provenance=provenance
        )
        verdict = PolicyEvalTool().evaluate(item, context=agent_ctx, session=db)  # type: ignore[arg-type]
        receipt_issue = _entry_receipt_issue(
            db, entry=e, agent_ctx=agent_ctx, owner_id=report.owner_id
        )
        flags, _failing = verdicts_to_flags(
            list(verdict.verdicts),
            entry_ref=ref,
            has_receipt=receipt_issue is None,
            category=category_value,
            receipt_issue=receipt_issue,
            entry_id=str(e.id),
            justification=e.justification,
        )
        snapshot = build_flags_snapshot(flags, entry_count=1)
        if int(snapshot.get("blocking_count", 0)) > 0:
            e.status = ExpenseStatus.flagged
        elif e.status == ExpenseStatus.flagged:
            e.status = ExpenseStatus.draft
        new_flags.extend(f for f in (snapshot.get("flags") or []) if isinstance(f, dict))

    existing = report.pre_submit_flags if isinstance(report.pre_submit_flags, dict) else {}
    # The report-wide check keys a vendorless line "entry_<n>": drop by id too.
    affected_ids = {str(e.id) for e in affected}
    kept = [
        f
        for f in (existing.get("flags") or [])
        if not (
            isinstance(f, dict)
            and (f.get("entry_ref") in refs or f.get("entry_id") in affected_ids)
        )
    ]
    merged = kept + new_flags
    entry_count = int(
        db.scalar(sa.select(sa.func.count(Expense.id)).where(Expense.report_id == report.id)) or 0
    )
    report.pre_submit_flags = {
        **existing,
        "flags": merged,
        "evaluated_entry_count": entry_count,
        "blocking_count": sum(
            1 for f in merged if isinstance(f, dict) and f.get("severity") == "blocking"
        ),
        "advisory_count": sum(
            1 for f in merged if isinstance(f, dict) and f.get("severity") == "advisory"
        ),
    }
    db.flush()


def _sync_linked_expense(
    db: Session,
    *,
    user: User,
    receipt: Receipt,
    field: str,
    value: object,
    agent_ctx: object,
) -> LinkedExpenseSync:
    """C7-17: propagate a receipt correction to its linked expense entry.

    Only an entry on a DRAFT report owned by the caller (same org) is mutated;
    the report total is recomputed with the expense service's derivation. A
    submitted/approved report is never touched — the outcome says so instead.
    Flush-only; the caller commits.

    Locking (C7R review — same pattern as the expense service's update_entry):
    the entry's report is locked ``FOR UPDATE`` (a concurrent submit waits for
    this sync, so a submitted report never holds a change its submit did not
    see), then the entry is re-read ``FOR UPDATE`` under that lock.  Moves lock
    the source report, so once it is held the entry's report_id is stable; if a
    concurrent move re-parented the entry before the lock was acquired, the
    sync follows it to its new report (bounded retries).  Should the entry keep
    moving, the entry is left untouched and the outcome is
    ``skipped_report_not_editable`` — the receipt correction itself still lands.
    """
    if receipt.expense_id is None:
        return "no_linked_expense"
    column = _ENTRY_FIELD_FOR_RECEIPT_FIELD.get(field)
    if column is not None and shared_expense_ids(receipt):
        # UAT2 hardening: a receipt split across lines (itemized siblings) is no
        # single line's claim — its total / date / vendor never overwrite a line.
        column = None

    locked = expense_service.lock_linked_entry(db, user=user, expense_id=receipt.expense_id)
    if column is None and (locked is None or locked == "moving"):
        return "not_applicable"
    if locked is None:
        logger.warning(
            "receipt.correct: linked expense of receipt=%s not owned by caller; not synced",
            receipt.id,
        )
        return "skipped_not_owner"
    if locked == "moving":
        logger.info(
            "receipt.correct: linked expense of receipt=%s kept moving between reports; "
            "entry not modified",
            receipt.id,
        )
        return "skipped_report_not_editable"
    entry, report = locked
    editable = report.status == ExpenseReportStatus.draft and report.withdrawn_at is None
    if column is None and not editable:
        return "not_applicable"
    if not editable:
        logger.info(
            "receipt.correct: linked expense %s is on a %s report; entry not modified",
            entry.id,
            report.status.value,
        )
        return "skipped_report_not_editable"
    if column in MILEAGE_COMPUTED_COLUMNS and is_mileage_entry(entry):
        # Product gap 2: a mileage entry's amount / currency / category are never
        # set from a receipt (MIL-01 computes them; the attach is refused too —
        # this covers a link made before that guard).
        column = None

    if column is None or getattr(entry, column) == _entry_value_for(field, value):
        # UAT2 review: the ENTRY is unchanged but its evidence is not (a typed
        # receipt date / vendor / location changes the receipt checks), so the
        # draft gets its pre-submit check re-run — its stored flags must never
        # describe the receipt as it was before the correction.  UAT2 defect D:
        # the Receipt Inbox too (its flags come from the same report-wide check).
        _refresh_report_checks(db, report=report, entry=entry, actor_id=user.id)
        return "not_applicable" if column is None else "unchanged"

    new_value = _entry_value_for(field, value)

    before = _entry_audit_snapshot(entry)
    old_ref = _flag_ref(entry)
    setattr(entry, column, new_value)
    if column == "category":
        clear_unfit_policy_fields(entry)  # product gap 2: no stale attendees / disclosure
    entry.updated_by = user.id
    db.flush()
    report.total_amount = _compute_total(db, report.id)
    report.updated_by = user.id
    db.flush()
    if entry.match_source == ExpenseMatchSource.receipt and report.title == _RECEIPT_INBOX_TITLE:
        # Only the Receipt Inbox carries the auto-draft's vendor/entry-id keyed
        # flags; an assembled report's flags are re-derived by its own pre-submit
        # check / the submit path. Degrade, never block: a policy-engine failure leaves the previous
        # verdict in place (the submit path re-evaluates every entry).
        try:
            with db.begin_nested():
                _refresh_entry_policy(
                    db,
                    agent_ctx=agent_ctx,
                    receipt=receipt,
                    entry=entry,
                    report=report,
                    old_ref=old_ref,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "receipt.correct: policy refresh failed for expense=%s; previous verdict kept",
                entry.id,
            )
    # UAT2: every draft (an assembled or returned report — reachable now that a
    # receipt can be attached to any entry — and, UAT2 defect D, the Receipt
    # Inbox, whose single-entry refresh above cannot see the line's twins) gets
    # the full pre-submit re-check and a version bump, so the submit gate never
    # reads stale flags.  On an evaluation failure the refresh keeps the flags
    # above plus a not_evaluated note.
    _refresh_report_checks(db, report=report, entry=entry, actor_id=user.id)
    record_audit(
        db,
        organization_id=user.organization_id,
        action="expense.updated",
        entity_type="expense",
        entity_id=entry.id,
        actor_id=user.id,
        before_state=before,
        after_state=_entry_audit_snapshot(entry),
    )
    return "updated"


@router.patch("/receipts/{receipt_id}", response_model=ReceiptResponse)
def correct_receipt(
    receipt_id: uuid.UUID,
    body: ReceiptCorrectionRequest,
    user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> ReceiptResponse:
    """Apply a user correction to a receipt field.

    The field must be one of the six extracted receipt fields and the value is
    validated per field (C7-16; invalid → 422). The pipeline flips provenance
    to 'user_edited' for the corrected field, and the correction is propagated
    to the linked draft expense entry + report total (C7-17); the response's
    ``linked_expense_sync`` reports the outcome. Raises 404 if the receipt is
    not found or not owned by the caller.
    """
    agent_ctx = build_agent_context(user, db)

    # C7R-18: an upload whose total was withheld drafted no entry. Remember
    # whether the amount was unknown before this correction, so entering it can
    # create that deferred draft (below). Scoping is enforced by
    # apply_user_correction (a foreign receipt 404s before anything is used).
    # The row is LOCKED (FOR UPDATE, held until the commit below) and re-read
    # fresh: two concurrent "enter the total" PATCHes serialise here, so the
    # second sees the first's amount + expense_id and never drafts a duplicate
    # entry (review round). It also prevents lost updates between concurrent
    # corrections of different fields (both rewrite the JSONB columns).
    # Lock order (UAT2 review): a LINKED receipt's report and entry are locked
    # BEFORE the receipt row — report → entry → receipt, the order attach and the
    # entry / receipt / report deletes use — so a concurrent delete and this
    # correction queue instead of deadlocking.  (The sync below re-acquires the
    # same locks, already held.)
    # UAT2 hardening: should the receipt be LINKED by another request between the
    # unlocked read and the receipt lock (e.g. a concurrent "enter the total"
    # drafted its entry), the receipt lock is taken inside a savepoint and
    # released by rolling it back, then the new entry's report → entry → receipt
    # are locked in order — the lock-state check below must see the report the
    # receipt backs NOW, held so a submit cannot slip in before the commit.
    pre: Receipt | None = None
    for _attempt in range(3):
        linked_expense_id = db.scalar(
            sa.select(Receipt.expense_id).where(
                Receipt.id == receipt_id,
                Receipt.organization_id == user.organization_id,
                Receipt.uploaded_by == user.id,
            )
        )
        if linked_expense_id is not None:
            held = expense_service.lock_linked_entry(db, user=user, expense_id=linked_expense_id)
            if held == "moving":
                # UAT2 hardening (revision): without the report lock a submit
                # could land between the lock-state check and the commit.
                raise ConflictError(
                    "This receipt's expense is being moved by another request; reload and retry."
                )
            # None: the entry is gone (the receipt's link is then cleared — the
            # re-read below sees it and retries) or not on a report the caller
            # owns (no lock to take; such a link never syncs — skipped_not_owner).
        savepoint = db.begin_nested()
        pre = db.scalar(
            sa.select(Receipt)
            .where(
                Receipt.id == receipt_id,
                Receipt.organization_id == user.organization_id,
                Receipt.uploaded_by == user.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if pre is None or pre.expense_id is None or pre.expense_id == linked_expense_id:
            savepoint.commit()
            break
        savepoint.rollback()  # releases the receipt row lock; re-lock in order
    else:
        raise ConflictError("This receipt was just changed by another request; reload and retry.")
    # "Unknown" = nothing a draft could have been made from: a withheld (null)
    # total, or one the gap-fill LLM inferred (C7R — never drafted at upload).
    amount_was_unknown = pre is not None and _draftable_amount(pre) is None

    try:
        receipt = apply_user_correction(
            db,
            context=agent_ctx,
            receipt_id=receipt_id,
            field=body.field,
            value=body.value,
        )
    except InvalidCorrectionError as exc:
        raise UnprocessableEntityError("Invalid receipt correction.") from exc
    except ReceiptNotFoundError as exc:
        raise NotFoundError("Receipt not found or not accessible.") from exc
    except ReceiptLockedError as exc:
        # UAT2 hardening: relied-on evidence is frozen (kept for audit, or on a
        # report that is submitted / has approval history).  Checked under the
        # report → entry → receipt locks taken above.
        raise ConflictError(str(exc), code=RECEIPT_LOCKED) from exc

    policy_verdict: PolicyVerdictSummary | None = None
    sync: LinkedExpenseSync
    if (
        # Product gap 8: picking a supported currency also unblocks the draft.
        body.field in ("amount", "currency")
        and amount_was_unknown
        and receipt.expense_id is None
        and receipt.ocr_status == OcrStatus.processed
        and EXPENSE_CREATE_OWN in agent_ctx.granted_permission_keys
    ):
        # The deferred auto-draft (FEATURE B) now that the total is known —
        # same helper, same savepoint/degrade contract as the upload path.
        try:
            with db.begin_nested():
                policy_verdict = _auto_draft_expense_from_receipt(
                    db, user=user, receipt=receipt, agent_ctx=agent_ctx
                )
        except Exception:  # noqa: BLE001 — degrade; the correction itself still lands
            policy_verdict = None
            logger.exception(
                "receipt.correct: deferred auto-draft failed for receipt=%s", receipt.id
            )
        sync = "drafted" if receipt.expense_id is not None else "no_linked_expense"
    else:
        sync = _sync_linked_expense(
            db,
            user=user,
            receipt=receipt,
            field=body.field,
            value=(receipt.extracted_fields or {}).get(body.field),
            agent_ctx=agent_ctx,
        )

    # Write audit row
    record_audit(
        db,
        organization_id=user.organization_id,
        action="receipt.corrected",
        entity_type="receipt",
        entity_id=receipt_id,
        actor_id=user.id,
        after_state={
            "field": body.field,
            "linked_expense_sync": sync,
            # C7R-18: the draft entry a "drafted" correction created.
            **({"expense_id": str(receipt.expense_id)} if sync == "drafted" else {}),
        },
    )

    # Single commit (receipt + linked entry + report total + audit rows)
    db.commit()

    response = _receipt_response(db, receipt)
    response.linked_expense_sync = sync
    response.policy_verdict = policy_verdict
    return response
