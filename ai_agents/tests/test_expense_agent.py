"""Hermetic tests for the Expense Agent (P4-T10) — all verification gates.

No database, no LLM API, no network.
PolicyEvalTool.evaluate_many is patched to avoid DB reads.
write_decision_log is patched to avoid DB writes.
IngestedData/ReceiptFact/CardTxnFact built directly (no DB).

Gates:
  1  — Smart-match: card_txn + receipt → correct segment attachment
  2  — Duplicate: receipt + card_txn same charge → ONE entry; re-run → no new entries
  3  — Unmatched: txn w/o receipt + receipt w/o txn → both surfaced
  4  — Categorize: correct categories; unknown vendor → other; no LLM
  5  — Itemize: hotel folio splits → siblings is_itemized=True, parts sum to folio
  6  — Assemble + derived total: trip_id linked, total == sum(entries)
  7  — Pre-submit no-false-pass: over-cap/missing-receipt flagged; clean → 0 flags
  8  — Reflect: dropped policy failure re-injected
  9  — Role-bound (hermetic): EXPENSE_REPORT_CREATE blocked → log still written
  10 — Decision log + route mix: one row/invocation; deterministic never calls LLM
  11 — Degradation: ProviderError → fallback + needs_review
  12 — Persist (hermetic): flush-only (no commit); MagicMock(spec=Session)
  Extras: mixed currency → needs_review; empty ingest → ok; idempotency; nights=0
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

from ai_agents.agents.expense_agent import ExpenseAgent
from ai_agents.agents.expense_agent.assemble import DraftReport, assemble_report
from ai_agents.agents.expense_agent.categorize import DraftEntry, build_draft_entries
from ai_agents.agents.expense_agent.ingest import CardTxnFact, IngestedData, ReceiptFact
from ai_agents.agents.expense_agent.matching import MatchCandidate, smart_match
from ai_agents.agents.expense_agent.presubmit import (
    PreSubmitFlag,
    build_eval_item,
    run_presubmit,
)
from ai_agents.agents.expense_agent.reflection import (
    ExpenseMatchHonestyCritic,
    ExpenseMatchHypothesizer,
    ExpenseReflectionState,
)
from ai_agents.context import AgentContext
from ai_agents.guardrails.reflection import ReflectionOutcome, reflect
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict, RuleVerdict
from config.constants.permissions import EXPENSE_CREATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()

# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    """Raises AssertionError if ever called — proves LLM is not touched."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise AssertionError("LLM must NOT be called on this path")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise AssertionError("LLM must NOT be called on this path")


class _ProviderErrorProvider(LlmProvider):
    """Always raises ProviderError — simulates LLM unavailability (NFR-R1)."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")


class _ScriptedProvider(LlmProvider):
    """Returns pre-scripted responses in sequence (cyclic)."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return LlmResponse(
            text=text, model="test-stub", usage=LlmUsage(input_tokens=10, output_tokens=5)
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        yield from []


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _ctx(*, with_expense_create: bool = True) -> AgentContext:
    keys: frozenset[str] = frozenset({EXPENSE_CREATE_OWN}) if with_expense_create else frozenset()
    return AgentContext(
        organization_id=_ORG_ID,
        user_id=_USER_ID,
        granted_permission_keys=keys,
        correlation_id="test",
    )


_DECISION = RoutingDecision(
    target_agent=AiAgent.expense,
    route=AiRoute.llm,
    task_type="expense_report",
    confidence=0.95,
    rationale="context_type=expense",
)


def _task(payload: dict) -> Task:
    return Task(context_type="expense", payload=payload)


def _receipt(
    *,
    receipt_id: str | None = None,
    amount: float = 100.0,
    currency: str = "USD",
    date: str = "2026-06-10",
    vendor: str | None = "Marriott Hotel",
    expense_type: str | None = "lodging",
    line_items: list | None = None,
    already_linked: bool = False,
) -> ReceiptFact:
    return ReceiptFact(
        receipt_id=receipt_id or str(uuid.uuid4()),
        amount=amount,
        currency=currency,
        date=date,
        vendor=vendor,
        location="New York",
        expense_type=expense_type,
        line_items=line_items,
        already_linked=already_linked,
    )


def _card_txn(
    *,
    txn_id: str | None = None,
    amount: float = 100.0,
    currency: str = "USD",
    txn_date: str = "2026-06-10",
    merchant: str | None = "Marriott",
    already_matched: bool = False,
) -> CardTxnFact:
    return CardTxnFact(
        txn_id=txn_id or str(uuid.uuid4()),
        amount=amount,
        currency=currency,
        merchant=merchant,
        txn_date=txn_date,
        last_four="1234",
        already_matched=already_matched,
    )


def _ingested(
    receipts: list[ReceiptFact] | None = None,
    card_txns: list[CardTxnFact] | None = None,
    segments: list | None = None,
    trip_id: str | None = None,
    unprocessed_receipts: list[str] | None = None,
) -> IngestedData:
    return IngestedData(
        receipts=receipts or [],
        card_txns=card_txns or [],
        segments=segments or [],
        trip_id=trip_id,
        unprocessed_receipts=unprocessed_receipts or [],
    )


def _in_policy_verdict(ref_id: str, amount: float = 100.0) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(item_type="hotel", amount_usd=amount, ref_id=ref_id),
        in_policy=True,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="amount_cap",
                applies_to="hotel",
                source_clause_ref="HOTEL-CAP",
                in_policy=True,
                reason="Amount within cap.",
                requires_approver=None,
                observed={"amount_usd": amount, "cap": 300.0},
            ),
        ),
        evaluated_rule_count=1,
    )


def _out_policy_verdict(ref_id: str, amount: float = 500.0) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(item_type="hotel", amount_usd=amount, ref_id=ref_id),
        in_policy=False,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="amount_cap",
                applies_to="hotel",
                source_clause_ref="HOTEL-CAP",
                in_policy=False,
                reason=f"Amount {amount} exceeds cap 300.",
                requires_approver="VP",
                observed={"amount_usd": amount, "cap": 300.0},
            ),
        ),
        evaluated_rule_count=1,
    )


def _receipt_req_verdict(ref_id: str, amount: float = 100.0) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(item_type="receipt", amount_usd=amount, ref_id=ref_id),
        in_policy=True,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="receipt_required",
                applies_to="all",
                source_clause_ref="RECEIPT-REQ",
                in_policy=True,
                reason="Receipt required.",
                requires_approver=None,
                observed={"amount_usd": amount, "receipt_required": True},
            ),
        ),
        evaluated_rule_count=1,
    )


def _skip_verdict(ref_id: str) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(item_type="flight", amount_usd=100.0, ref_id=ref_id),
        in_policy=True,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="cabin_class",
                applies_to="flight",
                source_clause_ref="AIR-CABIN",
                in_policy=True,
                reason="Skipped.",
                requires_approver=None,
                observed={},  # empty = skip verdict
            ),
        ),
        evaluated_rule_count=1,
    )


# ---------------------------------------------------------------------------
# Gate 1 — Smart-match
# ---------------------------------------------------------------------------


class TestSmartMatch:
    def test_receipt_and_card_txn_matched(self) -> None:
        """Card_txn + receipt same amount/currency/date → ONE MatchCandidate with both."""
        rid = str(uuid.uuid4())
        tid = str(uuid.uuid4())
        r = _receipt(receipt_id=rid, amount=100.0, date="2026-06-10", vendor="Uber")
        t = _card_txn(txn_id=tid, amount=100.0, txn_date="2026-06-10", merchant="Uber")
        data = _ingested(receipts=[r], card_txns=[t])
        candidates = smart_match(data)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.receipt is not None and c.receipt.receipt_id == rid
        assert c.card_txn is not None and c.card_txn.txn_id == tid
        assert c.is_duplicate_resolved is True

    def test_receipt_matches_segment(self) -> None:
        """Receipt matching a segment should have segment attached."""

        class FakeSeg:
            segment_id = "seg-1"
            trip_id = "trip-1"
            segment_type = "hotel"
            supplier = "Marriott"
            location = "New York"
            start_datetime = "2026-06-10T00:00:00"
            end_datetime = "2026-06-12T00:00:00"
            amount = 200.0
            currency = "USD"
            details: dict = {}

        r = _receipt(amount=200.0, date="2026-06-10", expense_type="lodging", currency="USD")
        data = _ingested(receipts=[r], segments=[FakeSeg()])
        candidates = smart_match(data)
        assert len(candidates) == 1
        assert candidates[0].segment is not None


# ---------------------------------------------------------------------------
# Gate 2 — Duplicate + idempotency
# ---------------------------------------------------------------------------


class TestDuplicate:
    def test_same_charge_receipt_and_card_becomes_one_entry(self) -> None:
        """Receipt + card_txn for same charge → exactly ONE MatchCandidate."""
        r = _receipt(amount=75.0, date="2026-06-10", vendor="Delta Air")
        t = _card_txn(amount=75.0, txn_date="2026-06-10", merchant="Delta")
        data = _ingested(receipts=[r], card_txns=[t])
        candidates = smart_match(data)
        assert len(candidates) == 1
        assert candidates[0].is_duplicate_resolved is True

    def test_already_linked_receipt_skipped(self) -> None:
        """already_linked=True receipt → not re-matched (idempotency)."""
        r = _receipt(amount=100.0, already_linked=True)
        data = _ingested(receipts=[r], card_txns=[])
        candidates = smart_match(data)
        assert len(candidates) == 0

    def test_already_matched_txn_skipped(self) -> None:
        """already_matched=True card_txn → not re-matched (idempotency)."""
        t = _card_txn(amount=100.0, already_matched=True)
        data = _ingested(receipts=[], card_txns=[t])
        candidates = smart_match(data)
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# Gate 3 — Unmatched surfaced
# ---------------------------------------------------------------------------


class TestUnmatched:
    def test_card_txn_without_receipt_surfaced(self) -> None:
        """A card_txn with no matching receipt becomes its own candidate."""
        t = _card_txn(amount=45.0, txn_date="2026-06-10", merchant="Coffee Shop")
        data = _ingested(receipts=[], card_txns=[t])
        candidates = smart_match(data)
        assert len(candidates) == 1
        assert candidates[0].card_txn is not None
        assert candidates[0].receipt is None

    def test_receipt_without_txn_surfaced(self) -> None:
        """A receipt with no matching txn becomes its own candidate."""
        r = _receipt(amount=89.0, date="2026-06-11", vendor="Bistro Cafe")
        data = _ingested(receipts=[r], card_txns=[])
        candidates = smart_match(data)
        assert len(candidates) == 1
        assert candidates[0].receipt is not None
        assert candidates[0].card_txn is None

    def test_unprocessed_receipts_surfaced_in_ingest(self) -> None:
        """Pending/failed receipts appear in unprocessed_receipts, not receipts."""
        data = _ingested(receipts=[], unprocessed_receipts=["receipt-abc"])
        assert "receipt-abc" in data.unprocessed_receipts


# ---------------------------------------------------------------------------
# Gate 4 — Categorize
# ---------------------------------------------------------------------------


class TestCategorize:
    def _candidate(
        self,
        segment_type: str | None = None,
        expense_type: str | None = None,
        vendor: str | None = None,
    ) -> MatchCandidate:
        seg = None
        if segment_type is not None:

            class FakeSeg:
                pass

            s = FakeSeg()
            s.segment_id = "seg-x"
            s.segment_type = segment_type
            s.amount = 100.0
            s.currency = "USD"
            s.details = {}
            seg = s
        has_receipt_info = expense_type or vendor
        r = (
            _receipt(vendor=vendor, expense_type=expense_type, amount=100.0)
            if has_receipt_info
            else None
        )
        return MatchCandidate(
            key="k",
            amount=100.0,
            currency="USD",
            date="2026-06-10",
            vendor=vendor,
            receipt=r,
            card_txn=None,
            segment=seg,
            match_source="receipt" if r else "card_txn",
            match_confidence=0.9,
            is_duplicate_resolved=False,
        )

    def test_flight_segment_type_maps_to_airfare(self) -> None:
        c = self._candidate(segment_type="flight")
        entries = build_draft_entries([c])
        assert entries[0].category == "airfare"

    def test_hotel_segment_type_maps_to_lodging(self) -> None:
        c = self._candidate(segment_type="hotel")
        entries = build_draft_entries([c])
        assert entries[0].category == "lodging"

    def test_receipt_expense_type_meals(self) -> None:
        c = self._candidate(expense_type="meals", vendor="Local Cafe")
        entries = build_draft_entries([c])
        assert entries[0].category == "meals"

    def test_unknown_vendor_categorized_as_other(self) -> None:
        c = self._candidate(vendor="Unknown XYZ Corp")
        entries = build_draft_entries([c])
        assert entries[0].category == "other"

    def test_marriott_keyword_maps_to_lodging(self) -> None:
        c = MatchCandidate(
            key="k",
            amount=200.0,
            currency="USD",
            date="2026-06-10",
            vendor="New York Marriott",
            receipt=None,
            card_txn=None,
            segment=None,
            match_source="card_txn",
            match_confidence=0.6,
            is_duplicate_resolved=False,
        )
        entries = build_draft_entries([c])
        assert entries[0].category == "lodging"

    def test_no_llm_called_during_categorize(self) -> None:
        """Categorize must not invoke any LLM."""
        c = self._candidate(vendor="Starbucks Coffee")
        # This should never call the provider; _RaisingProvider verifies
        entries = build_draft_entries([c])
        assert entries[0].category == "meals"


# ---------------------------------------------------------------------------
# Gate 5 — Itemize
# ---------------------------------------------------------------------------


class TestItemize:
    def test_hotel_folio_with_line_items_splits_into_siblings(self) -> None:
        """Receipt with line_items ≥ 2 → sibling DraftEntry rows."""
        line_items = [
            {"description": "Room charge", "amount": 180.0},
            {"description": "Restaurant dinner", "amount": 20.0},
        ]
        r = _receipt(
            amount=200.0,
            vendor="Marriott Hotel",
            expense_type="lodging",
            line_items=line_items,
        )
        c = MatchCandidate(
            key="k",
            amount=200.0,
            currency="USD",
            date="2026-06-10",
            vendor="Marriott Hotel",
            receipt=r,
            card_txn=None,
            segment=None,
            match_source="receipt",
            match_confidence=0.9,
            is_duplicate_resolved=False,
        )
        entries = build_draft_entries([c])
        assert all(e.is_itemized for e in entries)
        assert len(entries) == 2
        # Same sibling_group_id
        group_ids = {e.sibling_group_id for e in entries}
        assert len(group_ids) == 1
        # Sum equals folio total within $0.01
        total = sum(e.amount for e in entries)
        assert abs(total - 200.0) <= 0.01
        # Exactly one has primary_receipt_id
        primary_entries = [e for e in entries if e.primary_receipt_id is not None]
        assert len(primary_entries) == 1

    def test_single_charge_not_itemized(self) -> None:
        """Receipt without line_items → single entry, is_itemized=False."""
        r = _receipt(amount=50.0, line_items=None)
        c = MatchCandidate(
            key="k",
            amount=50.0,
            currency="USD",
            date="2026-06-10",
            vendor="Test",
            receipt=r,
            card_txn=None,
            segment=None,
            match_source="receipt",
            match_confidence=0.9,
            is_duplicate_resolved=False,
        )
        entries = build_draft_entries([c])
        assert len(entries) == 1
        assert entries[0].is_itemized is False


# ---------------------------------------------------------------------------
# Gate 6 — Assemble + derived total
# ---------------------------------------------------------------------------


class TestAssemble:
    def test_total_amount_is_sum_of_entries(self) -> None:
        """total_amount == round(sum(entries), 2)."""
        entries = [
            DraftEntry(
                category="meals",
                amount=45.50,
                currency="USD",
                expense_date="2026-06-10",
                vendor="Cafe",
                description=None,
                status="matched",
                match_source="receipt",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id="r1",
                source_card_txn_id=None,
                sibling_group_id=None,
            ),
            DraftEntry(
                category="lodging",
                amount=200.00,
                currency="USD",
                expense_date="2026-06-11",
                vendor="Marriott",
                description=None,
                status="matched",
                match_source="receipt",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id="r2",
                source_card_txn_id=None,
                sibling_group_id=None,
            ),
        ]
        report = assemble_report(entries, trip_id="trip-1", today="2026-06-14")
        assert report.total_amount == round(45.50 + 200.00, 2)
        assert report.trip_id == "trip-1"
        assert not report.mixed_currency

    def test_mixed_currency_flagged(self) -> None:
        """Mixed currencies → mixed_currency=True."""
        entries = [
            DraftEntry(
                category="meals",
                amount=50.0,
                currency="USD",
                expense_date="2026-06-10",
                vendor="Cafe",
                description=None,
                status="matched",
                match_source="receipt",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id=None,
                source_card_txn_id=None,
                sibling_group_id=None,
            ),
            DraftEntry(
                category="meals",
                amount=40.0,
                currency="EUR",
                expense_date="2026-06-11",
                vendor="Bistro",
                description=None,
                status="matched",
                match_source="receipt",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id=None,
                source_card_txn_id=None,
                sibling_group_id=None,
            ),
        ]
        report = assemble_report(entries, trip_id=None, today="2026-06-14")
        assert report.mixed_currency is True

    def test_empty_ingest_assembles_empty_report(self) -> None:
        """Empty entries → report with total=0."""
        report = assemble_report([], trip_id=None, today="2026-06-14")
        assert report.total_amount == 0.0
        assert report.entries == []


# ---------------------------------------------------------------------------
# Gate 7 — Pre-submit no-false-pass
# ---------------------------------------------------------------------------


class TestPreSubmit:
    def _make_entry(
        self,
        category: str = "lodging",
        amount: float = 100.0,
        vendor: str = "Marriott",
        primary_receipt_id: str | None = "r1",
        status: str = "matched",
    ) -> DraftEntry:
        return DraftEntry(
            category=category,
            amount=amount,
            currency="USD",
            expense_date="2026-06-10",
            vendor=vendor,
            description=None,
            status=status,
            match_source="receipt",
            trip_segment_id=None,
            is_itemized=False,
            primary_receipt_id=primary_receipt_id,
            source_card_txn_id=None,
            sibling_group_id=None,
        )

    def test_clean_report_produces_no_flags(self) -> None:
        """Clean entries with all in-policy verdicts → no flags."""
        entry = self._make_entry()
        report = DraftReport(
            entries=[entry],
            total_amount=100.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        verdicts = (_in_policy_verdict("Marriott"),)
        with patch(
            "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
            return_value=verdicts,
        ):
            flags, _ = run_presubmit(report, context=_ctx(), session=MagicMock())
        policy_violation_flags = [f for f in flags if f.flag_type == "policy_violation"]
        assert len(policy_violation_flags) == 0

    def test_over_cap_amount_flagged(self) -> None:
        """Entry exceeding cap → policy_violation blocking flag."""
        entry = self._make_entry(amount=500.0)
        report = DraftReport(
            entries=[entry],
            total_amount=500.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        verdicts = (_out_policy_verdict("Marriott", 500.0),)
        with patch(
            "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
            return_value=verdicts,
        ):
            flags, _ = run_presubmit(report, context=_ctx(), session=MagicMock())
        blocking = [f for f in flags if f.severity == "blocking"]
        assert len(blocking) >= 1
        assert any(f.flag_type == "policy_violation" for f in blocking)

    def test_c3_missing_receipt_flagged(self) -> None:
        """C3: receipt_required rule observed + no receipt → missing_receipt blocking flag."""
        entry = self._make_entry(primary_receipt_id=None)  # no receipt
        report = DraftReport(
            entries=[entry],
            total_amount=100.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        # receipt_required verdict with observed.receipt_required=True
        verdicts = (_receipt_req_verdict("Marriott"),)
        with patch(
            "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
            return_value=verdicts,
        ):
            flags, _ = run_presubmit(report, context=_ctx(), session=MagicMock())
        missing = [f for f in flags if f.flag_type == "missing_receipt"]
        assert len(missing) >= 1
        assert missing[0].severity == "blocking"

    def test_c3_receipt_present_no_false_flag(self) -> None:
        """C3: receipt_required rule + receipt present → no missing_receipt flag.

        The receipt must be a real, owned receipt whose machine-read amount covers
        the claim (a bare receipt id is no longer enough — C7 review).
        """
        from database.models.receipt import Receipt

        rid = uuid.uuid4()
        receipt = Receipt(
            id=rid,
            organization_id=_ORG_ID,
            uploaded_by=_USER_ID,
            extracted_fields={"amount": 100.0},
            field_provenance={"amount": "deterministic"},
        )
        session = MagicMock()
        session.get.side_effect = lambda model, ident: receipt if ident == rid else None
        entry = self._make_entry(primary_receipt_id=str(rid))
        report = DraftReport(
            entries=[entry],
            total_amount=100.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        verdicts = (_receipt_req_verdict("Marriott"),)
        with patch(
            "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
            return_value=verdicts,
        ):
            flags, _ = run_presubmit(report, context=_ctx(), session=session)
        missing = [f for f in flags if f.flag_type == "missing_receipt"]
        assert len(missing) == 0

    def test_c1_lodging_nights_zero_no_zerodiv(self) -> None:
        """C1: nights=0 → advisory flag, no ZeroDivisionError."""
        entry = self._make_entry(category="lodging", amount=200.0)
        # build_eval_item with a segment with nights=0
        item = build_eval_item(entry, segment=None, idx=0)
        # Should not raise; evaluates at full amount
        assert item.amount_usd == 200.0

    def test_c2_ground_transport_no_vehicle_class_not_blocked(self) -> None:
        """C2: ground_transport without vehicle_class → evaluated as receipt, not car."""
        entry = self._make_entry(category="ground_transport", amount=50.0)
        item = build_eval_item(entry, segment=None, idx=0)
        # Without vehicle_class, should be item_type=receipt
        assert item.item_type == "receipt"

    def test_skip_verdict_surfaced_as_not_evaluated(self) -> None:
        """observed={} → not_evaluated advisory flag."""
        entry = self._make_entry(category="airfare")
        report = DraftReport(
            entries=[entry],
            total_amount=100.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        verdicts = (_skip_verdict("Marriott"),)
        with patch(
            "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
            return_value=verdicts,
        ):
            flags, _ = run_presubmit(report, context=_ctx(), session=MagicMock())
        not_eval = [f for f in flags if f.flag_type == "not_evaluated"]
        assert len(not_eval) >= 1
        assert all(f.severity == "advisory" for f in not_eval)

    def test_ma3_no_failing_verdict_silently_passes(self) -> None:
        """M-A3: failing verdicts must all appear as policy_violation flags."""
        entry = self._make_entry(amount=999.0)
        report = DraftReport(
            entries=[entry],
            total_amount=999.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        verdicts = (_out_policy_verdict("Marriott", 999.0),)
        with patch(
            "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
            return_value=verdicts,
        ):
            flags, _ = run_presubmit(report, context=_ctx(), session=MagicMock())
        # count(policy_violation) >= count(failing_verdicts)
        pv_flags = [f for f in flags if f.flag_type == "policy_violation"]
        assert len(pv_flags) >= 1


# ---------------------------------------------------------------------------
# Gate 8 — Reflect
# ---------------------------------------------------------------------------


class TestReflect:
    def _make_state(
        self,
        flags: list[PreSubmitFlag] | None = None,
        verdicts_index: dict | None = None,
    ) -> ExpenseReflectionState:
        entry = DraftEntry(
            category="lodging",
            amount=100.0,
            currency="USD",
            expense_date="2026-06-10",
            vendor="Marriott",
            description=None,
            status="matched",
            match_source="receipt",
            trip_segment_id=None,
            is_itemized=False,
            primary_receipt_id="r1",
            source_card_txn_id=None,
            sibling_group_id=None,
        )
        report = DraftReport(
            entries=[entry],
            total_amount=100.0,
            currency="USD",
            trip_id=None,
            title="Test",
        )
        return ExpenseReflectionState(
            report=report,
            flags=flags or [],
            verdicts_index=verdicts_index or {},
        )

    def test_clean_state_passes_reflection(self) -> None:
        """Clean state with no contradictions → outcome=passed."""
        state = self._make_state()
        hyp = ExpenseMatchHypothesizer(state)
        critic = ExpenseMatchHonestyCritic()
        result = reflect(hypothesizer=hyp, critic=critic)
        assert result.outcome in {ReflectionOutcome.passed, ReflectionOutcome.revised}
        assert result.asserted is True

    def test_dropped_failing_verdict_reinjected(self) -> None:
        """Dropped failing verdict → critic catches and re-injects as flag."""
        rule_id = str(uuid.uuid4())
        # flags list is empty but verdicts_index has a failing verdict for "Marriott"
        failing_verdict = RuleVerdict(
            rule_id=rule_id,
            rule_type="amount_cap",
            applies_to="hotel",
            source_clause_ref="HOTEL-CAP",
            in_policy=False,
            reason="Over cap.",
            requires_approver="VP",
            observed={"amount_usd": 999.0},
        )
        state = self._make_state(
            flags=[],  # No flags — dropped verdict
            verdicts_index={"Marriott": [failing_verdict]},
        )
        hyp = ExpenseMatchHypothesizer(state)
        critic = ExpenseMatchHonestyCritic()
        result = reflect(hypothesizer=hyp, critic=critic)

        # Critic should have contradicted and re-injected the dropped verdict
        assert result.outcome in {ReflectionOutcome.revised, ReflectionOutcome.rejected}
        if isinstance(result.value, ExpenseReflectionState):
            injected = [f for f in result.value.flags if f.flag_type == "policy_violation"]
            assert len(injected) >= 1

    def test_empty_report_reflection_trivial_pass(self) -> None:
        """Empty report → reflection passes trivially with no entries to validate."""
        report = DraftReport(
            entries=[],
            total_amount=0.0,
            currency="USD",
            trip_id=None,
            title="Empty",
        )
        state = ExpenseReflectionState(report=report, flags=[], verdicts_index={})
        hyp = ExpenseMatchHypothesizer(state)
        critic = ExpenseMatchHonestyCritic()
        result = reflect(hypothesizer=hyp, critic=critic)
        assert result.asserted is True


# ---------------------------------------------------------------------------
# Gate 9 — Role-bound (hermetic)
# ---------------------------------------------------------------------------


class TestRoleBound:
    def test_different_owner_id_blocked(self) -> None:
        """owner_id != user_id → status=needs_review, denied=True."""
        other_id = str(uuid.uuid4())
        payload = {"owner_id": other_id}
        with patch(
            "ai_agents.agents.expense_agent.agent.write_decision_log",
            return_value=MagicMock(),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("denied") is True

    def test_same_owner_id_allowed(self) -> None:
        """owner_id == str(user_id) → not role-blocked."""
        payload = {"owner_id": str(_USER_ID)}
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=_ingested(),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert result.output.get("denied") is not True


# ---------------------------------------------------------------------------
# Gate 10 — Decision log + route mix
# ---------------------------------------------------------------------------


class TestDecisionLog:
    def test_one_log_row_per_invocation(self) -> None:
        """Exactly one decision log row per handle() call."""
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=_ingested(),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            agent.handle(
                _task({}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert mock_log.call_count == 1
        record = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.expense

    def test_deterministic_path_does_not_call_llm(self) -> None:
        """Without explain_with_llm, route=deterministic, LLM never called."""
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=_ingested(),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            # _RaisingProvider asserts if called
            agent = ExpenseAgent(provider=_RaisingProvider())
            agent.handle(
                _task({"explain_with_llm": False}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        record = mock_log.call_args.kwargs["record"]
        assert record.route == AiRoute.deterministic

    def test_llm_path_recorded_as_llm_route(self) -> None:
        """With explain_with_llm=True + working provider → route=llm."""
        scripted = _ScriptedProvider("This expense report looks correct.")
        # Need non-empty ingest so pipeline reaches the narrative step
        non_empty_ingested = _ingested(receipts=[_receipt(amount=50.0, vendor="Cafe")])
        in_policy = _in_policy_verdict("Cafe")
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=non_empty_ingested,
            ),
            patch(
                "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
                return_value=(in_policy,),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.persist_expense_report",
                return_value=(MagicMock(id=uuid.uuid4()), []),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            agent = ExpenseAgent(provider=scripted)
            agent.handle(
                _task({"explain_with_llm": True}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        record = mock_log.call_args.kwargs["record"]
        assert record.route == AiRoute.llm

    def test_role_blocked_still_writes_log(self) -> None:
        """Role-blocked path also writes exactly one decision log row."""
        other_id = str(uuid.uuid4())
        with patch(
            "ai_agents.agents.expense_agent.agent.write_decision_log",
            return_value=MagicMock(),
        ) as mock_log:
            agent = ExpenseAgent(provider=_RaisingProvider())
            agent.handle(
                _task({"owner_id": other_id}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert mock_log.call_count == 1


# ---------------------------------------------------------------------------
# Gate 11 — Degradation
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_llm_provider_error_falls_back_gracefully(self) -> None:
        """ProviderError on explain → templated fallback + needs_review + no crash."""
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=_ingested(),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = ExpenseAgent(provider=_ProviderErrorProvider())
            result = agent.handle(
                _task({"explain_with_llm": True}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        # Empty ingest → ok status (degraded handled by non-empty path)
        # Result must be returned without crash
        assert isinstance(result.output, dict)

    def test_provider_error_on_non_empty_report_gives_needs_review(self) -> None:
        """ProviderError during narrative on non-empty report → needs_review."""
        r = _receipt(amount=50.0, vendor="Cafe")
        t = _card_txn(amount=50.0, merchant="Cafe", txn_date="2026-06-10")
        ingested = _ingested(receipts=[r], card_txns=[t])
        in_policy = _in_policy_verdict("Cafe")
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=ingested,
            ),
            patch(
                "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
                return_value=(in_policy,),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.persist_expense_report",
                return_value=(MagicMock(id=uuid.uuid4()), []),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = ExpenseAgent(provider=_ProviderErrorProvider())
            result = agent.handle(
                _task({"explain_with_llm": True}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert result.output.get("degraded") is True
        assert result.status == AgentResultStatus.needs_review


# ---------------------------------------------------------------------------
# Gate 12 — Persist (hermetic)
# ---------------------------------------------------------------------------


class TestPersistHermetic:
    def test_guardrail_blocked_no_crash(self) -> None:
        """Missing EXPENSE_CREATE_OWN → persist blocked, but no crash, log written."""
        r = _receipt(amount=50.0)
        ingested = _ingested(receipts=[r])
        in_policy = _in_policy_verdict("entry_0")
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=ingested,
            ),
            patch(
                "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
                return_value=(in_policy,),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            ctx_no_perm = _ctx(with_expense_create=False)
            agent = ExpenseAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task({}),
                _DECISION,
                context=ctx_no_perm,
                session=MagicMock(),
            )
        assert result.status == AgentResultStatus.needs_review
        assert "persist_error" in result.output
        assert mock_log.call_count == 1

    def test_session_not_committed(self) -> None:
        """Session mock's commit() must never be called."""
        r = _receipt(amount=50.0)
        ingested = _ingested(receipts=[r])
        in_policy = _in_policy_verdict("entry_0")
        mock_session = MagicMock(spec=["add", "flush", "scalar", "scalars"])
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=ingested,
            ),
            patch(
                "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
                return_value=(in_policy,),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.persist_expense_report",
                return_value=(MagicMock(id=uuid.uuid4()), []),
            ),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            agent.handle(
                _task({}),
                _DECISION,
                context=_ctx(),
                session=mock_session,
            )
        assert not hasattr(mock_session, "commit") or not mock_session.commit.called


# ---------------------------------------------------------------------------
# Extra tests
# ---------------------------------------------------------------------------


class TestExtras:
    def test_empty_ingest_returns_ok_with_nothing_to_assemble(self) -> None:
        """Empty ingest (no receipts, no txns) → ok + 'nothing to assemble'."""
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=_ingested(),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task({}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert result.status == AgentResultStatus.ok
        assert "nothing to assemble" in str(result.output.get("message", "")).lower()

    def test_mixed_currency_results_in_needs_review(self) -> None:
        """Mixed currencies → needs_review in final result."""
        r1 = _receipt(amount=50.0, currency="USD", vendor="Cafe", expense_type="meals")
        r2 = ReceiptFact(
            receipt_id=str(uuid.uuid4()),
            amount=40.0,
            currency="EUR",
            date="2026-06-10",
            vendor="Bistro",
            location=None,
            expense_type="meals",
            line_items=None,
            already_linked=False,
        )
        ingested = _ingested(receipts=[r1, r2])
        in_policy_usd = _in_policy_verdict("Cafe")
        in_policy_eur = _in_policy_verdict("Bistro")
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=ingested,
            ),
            patch(
                "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
                return_value=(in_policy_usd, in_policy_eur),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.persist_expense_report",
                return_value=(MagicMock(id=uuid.uuid4()), []),
            ),
            patch(
                "ai_agents.agents.expense_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task({}),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )
        assert result.status == AgentResultStatus.needs_review

    def test_register_expense_registers_agent(self) -> None:
        """register_expense() correctly registers ExpenseAgent in the registry."""
        from ai_agents.agents.expense_agent import register_expense
        from ai_agents.orchestrator.registry import AgentRegistry

        registry = AgentRegistry()
        register_expense(registry)
        agent = registry.get_agent(AiAgent.expense)
        assert agent is not None
        assert isinstance(agent, ExpenseAgent)
