"""C7-22: the live-safe demo card-feed top-up.

Simulates the staging state after an acceptance run (all three seeded demo card
transactions consumed by smart-match: ``matched`` true and linked to an expense)
and checks that ``topup_demo_card_feed.top_up``:
- creates exactly one fresh UNMATCHED transaction per demo template (3);
- is a no-op on re-run;
- leaves the consumed rows untouched;
- touches nothing else (user password hashes, trips, reports, expenses,
  approvals, receipts).
Requires a migrated Postgres (skips otherwise).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense
from database.models.identity import User
from database.seeds.topup_demo_card_feed import DEMO_CARD_TRANSACTIONS, DEMO_EMPLOYEE_EMAIL

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = get_engine()
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    if "card_transactions" not in sa.inspect(eng).get_table_names():
        pytest.skip("schema not migrated")
    yield eng


def _counts(eng: Engine) -> dict[str, Any]:
    with eng.connect() as c:
        return {
            t: c.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in (
                "users",
                "trips",
                "expense_reports",
                "expenses",
                "approvals",
                "receipts",
                "audit_log",
                "policy_embeddings",
            )
        } | {
            "hashes": sorted(c.execute(sa.text("SELECT password_hash FROM users")).scalars().all())
        }


def _feed(eng: Engine) -> list[tuple[Any, ...]]:
    """(merchant, amount, date, matched, expense_id) for the demo employee, sorted."""
    with eng.connect() as c:
        rows = c.execute(
            sa.text(
                "SELECT t.id, t.merchant, t.amount, t.txn_date, t.matched, t.expense_id "
                "FROM card_transactions t JOIN users u ON u.id = t.user_id "
                "WHERE u.email = :e ORDER BY t.merchant, t.created_at, t.id"
            ),
            {"e": DEMO_EMPLOYEE_EMAIL},
        ).all()
    return [tuple(r) for r in rows]


def _unmatched_per_template(eng: Engine) -> dict[str, int]:
    out: dict[str, int] = {}
    for _id, merchant, _amt, _d, matched, expense_id in _feed(eng):
        if not matched and expense_id is None:
            out[merchant] = out.get(merchant, 0) + 1
    return out


@pytest.fixture(scope="module")
def staging_like(engine: Engine) -> Iterator[None]:
    """Full seed, then consume every demo card transaction as smart-match does."""
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()
    factory = get_sessionmaker()
    with factory() as s:
        employee = s.query(User).filter(User.email == DEMO_EMPLOYEE_EMAIL).one()
        expense_id = s.scalar(sa.select(Expense.id).limit(1))
        assert expense_id is not None, "seed should create at least one expense"
        txns = s.query(CardTransaction).filter(CardTransaction.user_id == employee.id).all()
        assert len(txns) >= len(DEMO_CARD_TRANSACTIONS)
        for t in txns:
            t.matched = True
            t.expense_id = expense_id
        s.commit()
    yield


def test_topup_restores_one_unmatched_per_template_and_touches_nothing_else(
    engine: Engine, staging_like: None
) -> None:
    from database.seeds.topup_demo_card_feed import top_up  # noqa: PLC0415

    assert _unmatched_per_template(engine) == {}
    consumed_before = _feed(engine)
    before = _counts(engine)

    result = top_up()

    assert result == {"created": 3, "available": 0}
    expected = {tpl.merchant: 1 for tpl in DEMO_CARD_TRANSACTIONS}
    assert _unmatched_per_template(engine) == expected
    after_feed = _feed(engine)
    assert len(after_feed) == len(consumed_before) + 3
    # Consumed rows are untouched (still matched + linked).
    assert set(consumed_before) <= set(after_feed)
    # Fresh rows carry the template's exact merchant / amount / date.
    fresh = [r for r in after_feed if r not in consumed_before]
    assert sorted((r[1], r[2], r[3]) for r in fresh) == sorted(
        (t.merchant, t.amount, t.txn_date) for t in DEMO_CARD_TRANSACTIONS
    )
    assert _counts(engine) == before

    # Idempotent: the feed is available, so a second run creates nothing.
    again = top_up()
    assert again == {"created": 0, "available": 3}
    assert _feed(engine) == after_feed
    assert _counts(engine) == before


def test_topup_only_refills_consumed_templates(engine: Engine, staging_like: None) -> None:
    from database.seeds.topup_demo_card_feed import top_up  # noqa: PLC0415

    top_up()  # make every template available first
    factory = get_sessionmaker()
    with factory() as s:
        employee = s.query(User).filter(User.email == DEMO_EMPLOYEE_EMAIL).one()
        pizza = (
            s.query(CardTransaction)
            .filter(
                CardTransaction.user_id == employee.id,
                CardTransaction.merchant == "Joe's Pizza NYC",
                CardTransaction.matched.is_(False),
            )
            .one()
        )
        pizza.matched = True  # consumed by a new smart-match
        s.commit()

    assert top_up() == {"created": 1, "available": 2}
    assert _unmatched_per_template(engine) == {tpl.merchant: 1 for tpl in DEMO_CARD_TRANSACTIONS}


def test_main_exits_zero(engine: Engine, staging_like: None) -> None:
    from database.seeds.topup_demo_card_feed import main  # noqa: PLC0415

    assert main() == 0


def test_main_exits_one_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing top-up must fail the boot (entrypoint `set -e`)."""
    from database.seeds import topup_demo_card_feed as mod  # noqa: PLC0415

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "top_up", _boom)
    assert mod.main() == 1


def test_no_demo_org_is_a_noop() -> None:
    """Absent demo org → None (exit 0), nothing created."""
    from database.seeds.topup_demo_card_feed import top_up  # noqa: PLC0415

    class _Query:
        def filter(self, *_a: object) -> _Query:
            return self

        def first(self) -> None:
            return None

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def query(self, *_a: object) -> _Query:
            return _Query()

    assert top_up(session_factory=_Session) is None
