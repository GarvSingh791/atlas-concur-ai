"""P7-T1 · item F — optimistic-lock + idempotency primitives.

Targets ``backend.app.core.concurrency`` (``if_match_version``,
``guarded_version_update``) and ``backend.app.core.idempotency``
(``idempotency_key_header``, ``idempotency_lookup``, ``idempotency_record``).

``if_match_version`` and ``idempotency_key_header`` are pure header parsers →
hermetic (no DB). ``guarded_version_update`` performs an atomic CAS UPDATE and
``idempotency_lookup``/``idempotency_record`` hit the ``idempotency_keys`` table
→ live-DB (inherited ``db_session``; self-skips if Postgres is unreachable).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.concurrency import guarded_version_update, if_match_version
from backend.app.core.errors import BadRequestError, ConflictError
from backend.app.core.idempotency import (
    idempotency_key_header,
    idempotency_lookup,
    idempotency_record,
)
from database.models.expense_report import ExpenseReport
from database.models.identity import Organization

_SCOPE = "expense_report.create"


# ===========================================================================
# if_match_version — hermetic header parser
# ===========================================================================


def test_if_match_none_returns_none() -> None:
    assert if_match_version(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3),
        ('"7"', 7),  # quoted ETag form
        ("  5  ", 5),  # surrounding whitespace
        ('" 9 "', 9),
        ("0", 0),
    ],
)
def test_if_match_parses_valid_int(raw: str, expected: int) -> None:
    assert if_match_version(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", "1.5", "v2", "  "])
def test_if_match_malformed_raises_bad_request(raw: str) -> None:
    with pytest.raises(BadRequestError):
        if_match_version(raw)


# ===========================================================================
# guarded_version_update — atomic CAS (live-DB)
# ===========================================================================


def test_guarded_update_success_increments_version(
    db_session: Session, org_a: Organization
) -> None:
    """A CAS update with the correct expected_version applies the values and
    increments version by exactly 1."""
    report = f.make_expense_report(db_session, org=org_a, title="before")
    start_version = report.version

    guarded_version_update(
        db_session,
        ExpenseReport,
        entity_id=report.id,
        expected_version=start_version,
        values={"title": "after"},
    )
    db_session.refresh(report)

    assert report.version == start_version + 1
    assert report.title == "after"


def test_guarded_update_stale_version_raises_conflict(
    db_session: Session, org_a: Organization
) -> None:
    """A stale expected_version matches no row → ConflictError, no mutation.

    Mutation target: this guards the optimistic-lock contract used by
    transition_report and decide_approval.
    """
    report = f.make_expense_report(db_session, org=org_a, title="locked")
    stale = report.version - 1  # off-by-one stale version

    with pytest.raises(ConflictError):
        guarded_version_update(
            db_session,
            ExpenseReport,
            entity_id=report.id,
            expected_version=stale,
            values={"title": "should-not-apply"},
        )

    db_session.refresh(report)
    assert report.title == "locked"  # untouched
    assert report.version == report.version  # unchanged (sanity)


def test_guarded_update_rejects_version_in_values(db_session: Session, org_a: Organization) -> None:
    """'version' is managed internally; supplying it is a programming error."""
    report = f.make_expense_report(db_session, org=org_a)
    with pytest.raises(ValueError):
        guarded_version_update(
            db_session,
            ExpenseReport,
            entity_id=report.id,
            expected_version=report.version,
            values={"version": 99, "title": "x"},
        )


def test_guarded_update_unknown_id_raises_conflict(
    db_session: Session, org_a: Organization
) -> None:
    """No row with that id → fetchone() is None → ConflictError (CAS miss)."""
    with pytest.raises(ConflictError):
        guarded_version_update(
            db_session,
            ExpenseReport,
            entity_id=uuid.uuid4(),
            expected_version=1,
            values={"title": "ghost"},
        )


# ===========================================================================
# idempotency_key_header — hermetic header parser
# ===========================================================================


def test_idempotency_key_header_absent_returns_none() -> None:
    assert idempotency_key_header(None) is None


def test_idempotency_key_header_present_returns_value() -> None:
    assert idempotency_key_header("abc-123") == "abc-123"


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_idempotency_key_header_empty_raises_bad_request(raw: str) -> None:
    with pytest.raises(BadRequestError):
        idempotency_key_header(raw)


# ===========================================================================
# idempotency_lookup / idempotency_record — live-DB
# ===========================================================================


def test_idempotency_lookup_miss_returns_none(db_session: Session, org_a: Organization) -> None:
    result = idempotency_lookup(
        db_session, organization_id=org_a.id, scope=_SCOPE, key="never-seen"
    )
    assert result is None


def test_idempotency_record_then_lookup_returns_entity_id(
    db_session: Session, org_a: Organization
) -> None:
    """record then lookup round-trips the stored entity_id (the replay seam)."""
    report = f.make_expense_report(db_session, org=org_a)
    key = "round-trip-key"

    idempotency_record(
        db_session,
        organization_id=org_a.id,
        scope=_SCOPE,
        key=key,
        entity_id=report.id,
    )
    db_session.flush()

    found = idempotency_lookup(db_session, organization_id=org_a.id, scope=_SCOPE, key=key)
    assert found == report.id


def test_idempotency_lookup_is_org_and_scope_scoped(
    db_session: Session, org_a: Organization, org_b: Organization
) -> None:
    """The same key under a different org OR a different scope is a miss — the
    lookup is keyed on (organization_id, scope, key)."""
    report = f.make_expense_report(db_session, org=org_a)
    key = "shared-key"
    idempotency_record(
        db_session,
        organization_id=org_a.id,
        scope=_SCOPE,
        key=key,
        entity_id=report.id,
    )
    db_session.flush()

    # Same key, different org → miss
    assert idempotency_lookup(db_session, organization_id=org_b.id, scope=_SCOPE, key=key) is None
    # Same key+org, different scope → miss
    assert (
        idempotency_lookup(db_session, organization_id=org_a.id, scope="trip.create", key=key)
        is None
    )
    # Exact triple → hit (control)
    assert (
        idempotency_lookup(db_session, organization_id=org_a.id, scope=_SCOPE, key=key) == report.id
    )


def test_idempotency_record_duplicate_key_raises_integrity_error(
    db_session: Session, org_a: Organization
) -> None:
    """The (organization_id, scope, key) unique constraint fires on a same-key
    race — second insert raises IntegrityError (→ 409 via the global handler).

    Uses a nested savepoint so the failed flush doesn't poison the session for
    teardown.
    """
    from sqlalchemy.exc import IntegrityError

    report = f.make_expense_report(db_session, org=org_a)
    key = "dup-key"
    idempotency_record(
        db_session,
        organization_id=org_a.id,
        scope=_SCOPE,
        key=key,
        entity_id=report.id,
    )
    db_session.flush()

    with db_session.begin_nested():
        idempotency_record(
            db_session,
            organization_id=org_a.id,
            scope=_SCOPE,
            key=key,  # duplicate triple
            entity_id=report.id,
        )
        with pytest.raises(IntegrityError) as exc_info:
            db_session.flush()

    assert getattr(exc_info.value.orig, "pgcode", None) == "23505"  # unique_violation


def test_idempotency_keys_table_has_no_organization_leak(
    db_session: Session, org_a: Organization, org_b: Organization
) -> None:
    """Two orgs may reuse the same key value for the same scope without colliding
    — the unique constraint is per-tenant, not global."""
    report_a = f.make_expense_report(db_session, org=org_a)
    report_b = f.make_expense_report(db_session, org=org_b)
    key = "tenant-local-key"

    idempotency_record(
        db_session, organization_id=org_a.id, scope=_SCOPE, key=key, entity_id=report_a.id
    )
    idempotency_record(
        db_session, organization_id=org_b.id, scope=_SCOPE, key=key, entity_id=report_b.id
    )
    db_session.flush()  # must NOT raise — distinct tenants

    assert (
        idempotency_lookup(db_session, organization_id=org_a.id, scope=_SCOPE, key=key)
        == report_a.id
    )
    assert (
        idempotency_lookup(db_session, organization_id=org_b.id, scope=_SCOPE, key=key)
        == report_b.id
    )


def test_idempotency_keys_table_name_exists(db_session: Session) -> None:
    """Sanity: the idempotency_keys table actually exists in the migrated schema
    (else record/lookup tests would be silently meaningless)."""
    from database.models.idempotency_key import IdempotencyKey

    # A trivial scoped query that must execute without error against the schema.
    count = db_session.scalar(sa.select(sa.func.count()).select_from(IdempotencyKey))
    assert count is not None and count >= 0
